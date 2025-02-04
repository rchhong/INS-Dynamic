import os, sys
import pdb
import numpy as np
import imageio
import json
import random
import time
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange

import matplotlib.pyplot as plt

from models.sampler import StratifiedSampler, ImportanceSampler
from models.renderer import VolumetricRenderer
from models.nerf_mlp import NeRFMLP, EmbedMLP
from pdb import set_trace as st

from utils.error import *

class NeRFNet(nn.Module):

    def __init__(self, netdepth=8, netwidth=256, netdepth_fine=8, netwidth_fine=256, no_skip=False, act_fn="relu", N_samples=64, N_importance=64,
        viewdirs=True, use_embed=True, multires=10, multires_views=4, ray_chunk=1024*32, pts_chuck=1024*64,
        perturb=1., raw_noise_std=0., fix_param=False, zero_viewdir=False, embed_mlp=False, offset_mlp=False, embed_posembed=False, stl_num=None,
        is_dynamic=False, xyz_min=None, xyz_max=None, num_voxels=0, num_voxels_base=0, num_voxel_grids=0,
        multires_times=0, multires_grid=0, deformation_depth=0):

        super().__init__()
        self.fix_coarse, self.fix_fine = fix_param
        # Create sampler
        self.N_samples, self.N_importance = N_samples, N_importance
        self.point_sampler = StratifiedSampler(N_samples, perturb=perturb, lindisp=False, pytest=False)
        self.importance_sampler = None
        if N_importance > 0:
            self.importance_sampler = ImportanceSampler(N_importance, perturb=perturb, lindisp=False, pytest=False)

        # Create transformer
        # self.cam_transformer = None
        # if args.num_cameras > 0:
        #     self.cam_transformer = CameraTransformer(args.num_cameras, trainable=args.trainable_cams)

        # Ray renderer
        self.renderer = VolumetricRenderer()

        # Maximum number of rays to process simultaneously. Used to control maximum memory usage. Does not affect final results.
        self.chunk = ray_chunk
        # Save if use view directions (which cannot be changed after building networks)
        self.use_viewdirs = viewdirs

        # net params
        input_ch = 3
        output_ch = 4
        skips = [4]

        self.nerf = NeRFMLP(input_dim=3, output_dim=4, net_depth=netdepth, net_width=netwidth, no_skip=no_skip, act_fn=act_fn, skips=[4],
                viewdirs=viewdirs, use_embed=use_embed, multires=multires, multires_views=multires_views, netchunk=pts_chuck,
                is_dynamic=is_dynamic, xyz_min=xyz_min, xyz_max=xyz_max, num_voxels=num_voxels, num_voxels_base=num_voxels_base, num_voxel_grids=num_voxel_grids,
                multires_times=multires_times, multires_grid=multires_grid, deformation_depth=deformation_depth)
        if self.fix_coarse == True or self.fix_coarse == "True":
            print(f"> Fix NeRF Coarse")
            for p in self.nerf.mlp.parameters():
                p.requires_grad = False

        self.nerf_fine = self.nerf
        if N_importance > 0:
            self.nerf_fine = NeRFMLP(input_dim=3, output_dim=4, net_depth=netdepth_fine, net_width=netwidth_fine, no_skip=no_skip, act_fn=act_fn, skips=[4],
                viewdirs=viewdirs, use_embed=use_embed, multires=multires, multires_views=multires_views, netchunk=pts_chuck,
                zero_viewdir=zero_viewdir, embed_mlp=embed_mlp, offset_mlp=offset_mlp, embed_posembed=embed_posembed, stl_num=stl_num,
                is_dynamic=is_dynamic, xyz_min=xyz_min, xyz_max=xyz_max, num_voxels=num_voxels, num_voxels_base=num_voxels_base, num_voxel_grids=num_voxel_grids,
                multires_times=multires_times, multires_grid=multires_grid, deformation_depth=deformation_depth)
            if self.fix_fine == True or self.fix_fine == "True":
                print(f"> Fix NeRF Fine")
                for p in self.nerf_fine.mlp.parameters():
                    p.requires_grad = False

        # render parameters
        self.render_kwargs_train = {
            'N_importance': N_importance,
            'N_samples': N_samples,
            'perturb': perturb,
            'raw_noise_std': raw_noise_std,
            'retraw': True, 'retpts': False
        }

        # copy from train rendering first
        self.render_kwargs_test = self.render_kwargs_train.copy()
        # no perturbation
        self.render_kwargs_test['perturb'] = 0.
        self.render_kwargs_test['raw_noise_std'] = 0.

    def _set_tinuvox_grid_resolution(self, num_voxels):
        self.nerf._set_tinuvox_grid_resolution(num_voxels)
        if N_importance > 0:
            self.nerf_fine._set_tinuvox_grid_resolution(num_voxels)

    def render_rays(self, rays_o, rays_d, near, far, viewdirs=None, stl_idx=None, times=None, raw_noise_std=0.,
        verbose=False, retraw = False, retpts=False, pytest=False, **kwargs):
        """Volumetric rendering.
        Args:
          ray_o: origins of rays. [N_rays, 3]
          ray_d: directions of rays. [N_rays, 3]
          near: the minimal distance. [N_rays, 1]
          far: the maximal distance. [N_rays, 1]
          raw_noise_std: If True, add noise on raw output from nn
          verbose: bool. If True, print more debugging info.
          times: float. The times for each frame for dynamic NeRF data [N_rays, 1]
        Returns:
          rgb: [N_rays, 3]. Estimated RGB color of a ray. Comes from fine model.
          raw: [N_rays, N_samples, C]. Raw predictions from model.
          pts: [N_rays, N_samples, 3]. Sampled points.
          rgb0: See rgb_map. Output for coarse model.
          raw0: See raw. Output for coarse model.
          pts0: See acc_map. Output for coarse model.
          z_std: [N_rays]. Standard deviation of distances along ray for each sample.
        """
        bounds = torch.cat([near, far], -1) # [N_rays, 2]
        # print("rays_o: ", rays_o.shape)
        # print("times: ", times.shape)
        # Primary sampling
        pts, z_vals, _ = self.point_sampler(rays_o, rays_d, bounds, **kwargs)  # [N_rays, N_samples, 3]

        # print(pts.shape)
        raw = self.nerf(pts, viewdirs, times = times)
        ret = self.renderer(raw, z_vals, rays_d, raw_noise_std=raw_noise_std, pytest=pytest)

        # Buffer raw/pts
        if retraw:
            ret['raw'] = raw
        if retpts:
            ret['pts'] = pts

        # Secondary sampling
        N_importance = kwargs.get('N_importance', self.N_importance)
        if (self.importance_sampler is not None) and (N_importance > 0):
            # backup coarse model output
            ret0 = ret

            # resample
            pts, z_vals, sampler_extras = self.importance_sampler(rays_o, rays_d, z_vals, **ret, **kwargs) # [N_rays, N_samples + N_importance, 3]
            # obtain raw data
            raw = self.nerf_fine(pts, viewdirs, stl_idx=stl_idx, times=times)
            # render raw data
            ret = self.renderer(raw, z_vals, rays_d, raw_noise_std=raw_noise_std, pytest=pytest)

            # Buffer raw/pts
            if retraw:
                ret['raw'] = raw
            if retpts:
                ret['pts'] = pts

            # compute std of resampled point along rays
            ret['z_std'] = torch.std(sampler_extras['z_samples'], dim=-1, unbiased=False)  # [N_rays]

            # buffer coarse model output
            for k in ret0:
                ret[k+'0'] = ret0[k]

        return ret

    # def forward(self, ray_batch, bound_batch, times=None, stl_idx=None, test=False, **kwargs):
    def forward(self, rays_o, rays_d, times, bound_batch, stl_idx=None, test=False, **kwargs):
        """Render rays
        Args:
          ray_batch: array of shape [2, batch_size, 3]. Ray origin and direction for
            each example in batch.
          times: array of shape [batch_size, 2, H, W, 3].  Time each image was taken
        Returns:
          ret_all includes the following returned values:
          rgb_map: [batch_size, 3]. Predicted RGB values for rays.
          raw: [batch_size, N_sample, C]. Raw data of each point.
          weight_map: [batch_size, N_sample, C]. Convert raw to weight scale (0-1).
          acc_map: [batch_size]. Accumulated opacity (alpha) along a ray.
        """

        # Render settings
        if not test:
            render_kwargs = self.render_kwargs_train.copy()
            render_kwargs.update(kwargs)
        else:
            render_kwargs = self.render_kwargs_test.copy()
            render_kwargs.update(kwargs)

        # # Disentangle ray batch
        rays_o, rays_d = rays_o.squeeze(0), rays_d.squeeze(0)
        times = times.squeeze(0)
        # # TODO: this line is not compatible with batch size > 1
        # rays_o, rays_d = ray_batch.squeeze(0) #[2,1,3] -> [1,3] [1,3] squeeze out batch dim
        # # rays_o, rays_d = ray_batch #[2,1,3] -> [1,3] [1,3] don't squeeze out batch dim
        # assert rays_o.shape == rays_d.shape

        # # print("ray_batch: ", ray_batch.shape)
        # # print("rays_o: ", rays_o.shape)
        # # print("rays_d: ", rays_d.shape)
        # # Flatten ray batch
        # old_shape = rays_d.shape # [..., 3(+id)]
        # rays_o = torch.reshape(rays_o, [-1,rays_o.shape[-1]]).float()
        # rays_d = torch.reshape(rays_d, [-1,rays_d.shape[-1]]).float()

        # # Flatten time
        # if(times is not None):
        #     times = torch.reshape(times[:, 0, ...], [-1,times.shape[-1]]).float()

        # Provide ray directions as input
        if self.use_viewdirs:
            viewdirs = rays_d
            viewdirs = viewdirs / torch.norm(viewdirs, dim=-1, keepdim=True)
            viewdirs = torch.reshape(viewdirs, [-1, viewdirs.shape[-1]]).float()

        # Disentangle bound batch
        near, far = bound_batch
        if isinstance(near, int) or isinstance(near, float):
            near = near * torch.ones_like(rays_d[...,:1], dtype=torch.float)
        if isinstance(far, int) or isinstance(far, float):
            far = far * torch.ones_like(rays_d[...,:1], dtype=torch.float)

        # Batchify rays
        all_ret = {}
        for i in range(0, rays_o.shape[0], self.chunk):
            end = min(i+self.chunk, rays_o.shape[0])
            chunk_o, chunk_d = rays_o[i:end], rays_d[i:end]
            chunk_n, chunk_f = near[i:end], far[i:end]
            chunk_v = viewdirs[i:end] if self.use_viewdirs else None
            chunk_t = times[i:end] if times is not None else None
            # Render function
            ret = self.render_rays(chunk_o, chunk_d, chunk_n, chunk_f, viewdirs=chunk_v, stl_idx=stl_idx, times = chunk_t, **render_kwargs)
            for k in ret:
                if k not in all_ret:
                    all_ret[k] = []
                all_ret[k].append(ret[k])
        all_ret = {k : torch.cat(all_ret[k], 0) for k in all_ret}

        # Unflatten
        # for k in all_ret:
        #     k_sh = [1] + list(old_shape[:-1]) + list(all_ret[k].shape[1:])
        #     all_ret[k] = torch.reshape(all_ret[k], k_sh) # [input_rays_shape, per_ray_output_shape]

        return all_ret

    # query raw data for points
    def forward_pts(self, pts_batch, test=False, **kwargs):
        raw = self.nerf(pts_batch)
#         raw = 1.0 - torch.exp(-F.relu(raw))
        return raw
