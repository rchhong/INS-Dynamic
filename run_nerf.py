from email.policy import default
import os, sys
import math, time, random, shutil

import numpy as np

import imageio
import json

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm, trange

import matplotlib.pyplot as plt

from utils.config import *
from utils.misc import *

from data.datasets import BatchNeRFDataset
from data.datasets import ExhibitNeRFDataset
from data.datasets import PatchNeRFDataset
# from data.datasets import BatchNeRFDataset as PatchNeRFDataset
from data.collater import Ray_Batch_Collate, Image_Batch_Collate
from models.nerf_net import NeRFNet
from engines.lr import LRScheduler
from engines.trainer import train_one_epoch, train_one_epoch_dynamic, save_checkpoint
from engines.eval import evaluate, render_video, linear_eval
from models.vgg import Vgg16
from models.transformer_net import TransformerNet
from pdb import set_trace as st
from models.tineuvox import compute_bbox_by_cam_frustrm
BASE_DIR = os.path.abspath(os.path.dirname(__file__))

# TODO: Train a TiNuVox Instance and then fix this, then utilze as the content-implicit module

'''How to use:
python run_nerf.py --action train --gpuid 1  --batch_size=1   --config configs/fern.txt  --N_iters 250000  \
    --expname stylenerf  --ckpt_path logs/fern/checkpoints/00200000.ckpt  --data_path datasets/nerf_llff_data/fern_stylenerf
'''
def create_arg_parser():
    parser = configargparse.ArgumentParser()
    parser.add_argument('--config', is_config_file=True,
                        help='config file path')
    parser.add_argument("--expname", type=str, default="tmp",
                        help='experiment name')
    parser.add_argument("--basedir", type=str, default='./logs/',
                        help='where to store ckpts and logs')
    parser.add_argument("--data_path", "--datadir", type=str, default='./datasets/nerf_synthetic/lego_100test/',
                        help='input data directory')
    parser.add_argument("--gpuid", type=int, default=0,
                        help='gpu id for cuda')
    parser.add_argument("--eval", action='store_true',
                        help='only evaluate without training')

    parser.add_argument("--save_rays", action='store_true',
                        help='save rays, near, far for visualization')
    parser.add_argument("--save_pts", action='store_true',
                        help='save point samples for visualization')

    # Training options
    parser.add_argument("--netdepth", type=int, default=8,
                        help='layers in network')
    parser.add_argument("--netwidth", type=int, default=256,
                        help='channels per layer')
    parser.add_argument("--netdepth_fine", type=int, default=8,
                        help='layers in fine network')
    parser.add_argument("--netwidth_fine", type=int, default=256,
                        help='channels per layer in fine network')
    parser.add_argument("--no_skip", action='store_true',
                        help='with or without concat within MLP')
    parser.add_argument("--act_fn", type=str, default="relu",
                        help='activation function for MLP')
    parser.add_argument("--N_iters", type=int, default=200000,
                        help='max iteration number (number of iteration to finish training)')
    parser.add_argument("--batch_size", "--N_rand", type=int, default=32*32*4,
                        help='batch size (number of random rays per gradient step)')
    parser.add_argument("--num_devices", type=int, default=2,
                        help='number of GPUs (for batching)')
    parser.add_argument("--lrate", type=float, default=5e-4,
                        help='learning rate')
    parser.add_argument("--ray_chunk", type=int, default=1024*32,
                        help='number of rays processed in parallel, decrease if running out of memory')
    parser.add_argument("--pts_chunk", type=int, default=1024*256,
                        help='number of pts sent through network in parallel, decrease if running out of memory')
    parser.add_argument("--no_batching", action='store_true',
                        help='only take random rays from 1 image at a time')
    parser.add_argument("--verbose", action='store_true',
                        help='print more when training')

    # hyper-parameter for learning scheduler
    parser.add_argument("--decay_step", type=int, default=250,
                        help='exponential learning rate decay iteration (in 1000 steps)')
    parser.add_argument("--decay_rate", type=float, default=0.1,
                        help='exponential learning rate decay scale')

    # reload option
    parser.add_argument("--no_reload", action='store_true',
                        help='do not reload weights from saved ckpt')
    parser.add_argument("--ckpt_path", type=str, default='',
                        help='specific weights npy file to reload for coarse network')
    parser.add_argument("--teach_ckpt_path", type=str, default='',
                        help='')

    parser.add_argument("--pin_mem", action='store_true', default=True,
                        help='turn on pin memory for data loading')
    parser.add_argument("--no_pin_mem", action='store_false', dest='pin_memory',
                        help='turn off pin memory for data loading')
    parser.set_defaults(pin_mem=True)
    parser.add_argument("--num_workers", type=int, default=8,
                        help='number of workers used for data loading')

    # rendering options
    parser.add_argument("--N_samples", type=int, default=64,
                        help='number of coarse samples per ray')
    parser.add_argument("--N_importance", type=int, default=64,
                        help='number of additional fine samples per ray')
    parser.add_argument("--perturb", type=float, default=1.,
                        help='set to 0. for no jitter, 1. for jitter')
    parser.add_argument("--use_viewdirs", action='store_true', default=True,
                        help='enable full 5D input, using 3D without view dependency')
    parser.add_argument("--no_viewdirs", action='store_false', dest='use_viewdirs',
                        help='disable full 5D input, using 3D without view dependency')
    parser.set_defaults(use_viewdirs=True)
    parser.add_argument("--use_embed", action='store_true', default=True,
                        help='turn on positional encoding')
    parser.add_argument("--no_embed", action='store_false', dest='use_embed',
                        help='turn on positional encoding')
    parser.set_defaults(use_embed=True)
    parser.add_argument("--multires", type=int, default=10,
                        help='log2 of max freq for positional encoding (3D location)')
    parser.add_argument("--multires_views", type=int, default=4,
                        help='log2 of max freq for positional encoding (2D direction)')
    parser.add_argument("--raw_noise_std", type=float, default=0.,
                        help='std dev of noise added to regularize sigma_a output, 1e0 recommended')
    parser.add_argument("--rgb_weight", type=float, default=1.,
                        help='NeRF rendering loss weight')
    parser.add_argument("--content_weight", type=float, default=1e6,
                        help='NeRF rendering loss weight')
    parser.add_argument("--style_weight", type=float, default=1e8,
                        help='NeRF rendering loss weight')
    parser.add_argument("--perceptual_weight", type=float, default=1.,
                        help='NeRF rendering loss weight')

    # additional training options
    # parser.add_argument("--no_camera_id", action='store_true',
    #                     help='do not concat camera id with each ray')
    # parser.add_argument("--trainable_cams", action='store_true',
    #                     help='optimize camera pose jointly')
    # parser.add_argument("--prior_loss", type=str, default='none',
    #                     help='priors on the volumetric reconstruction')
    # parser.add_argument("--prior_coeff", type=float, default=0.001,
    #                     help='coefficient for the prior loss')

    # dataset options
    parser.add_argument("--dataset_type", type=str, default='nerf',
                        help='options: nerf / point cloud')
    parser.add_argument("--subsample", type=int, default=0,
                    help='subsampling rate if applicable')

    # corruptions
    parser.add_argument("--corrupt_cams", action='store_true',
                        help='whether corrupt camera extrinsics using a perturbation')
    parser.add_argument("--corrupt_cams_t", type=float, default=0.1,
                        help='how large are perturbation in rotation degree')
    parser.add_argument("--corrupt_cams_r", type=float, default=5.0,
                        help='how large are perturbation in rotation degree')
    parser.add_argument("--noise_level", type=float, default=0.1,
                        help='how strong are the gaussian noises added to corrupt images')

    # logging/saving options
    parser.add_argument("--i_print",   type=int, default=200,
                        help='frequency of console/tensorboard printout and metric loggin')
    parser.add_argument("--i_img",     type=int, default=10000,
                        help='frequency of tensorboard image logging')
    parser.add_argument("--log_img_idx", type=int, default=0,
                    help='the view idx used for logging while testing')
    parser.add_argument("--i_weights", type=int, default=10000,
                        help='frequency of weight ckpt saving')
    parser.add_argument("--i_testset", type=int, default=10000,
                        help='frequency of testset saving')
    parser.add_argument("--i_video",   type=int, default=25000,
                        help='frequency of render_poses video saving')
    parser.add_argument("--view0_only", action="store_true",
                        help='add style loss only to train view 0')
    parser.add_argument("--patch_size",   type=int, default=48,
                        help='patch size for each style and content image')
    parser.add_argument('--loss_terms', nargs='*', default=["coarse","fine","style_v_all","density"],
                        help="how many loss terms")
    parser.add_argument('--style_path',type=str, default=None,
                        help="style path")
    parser.add_argument("--fix_param", nargs='*', default=[False, False],
                        help='fix the weight of nerf')
    parser.add_argument("--with_mask", action='store_true', default=False,
                        help='apply mask on style nerf')
    parser.add_argument("--with_teach", action='store_true', default=False,
                        help='apply teacher student model')
    parser.add_argument("--d_weight",   type=float, default=1e8,
                        help='frequency of render_poses video saving')
    parser.add_argument("--sphere_style", default=None, type=str,
                        help='use sphere style patch')
    parser.add_argument("--zero_viewdir", action='store_true', default=False,
                        help='set viewdir as all zero')
    parser.add_argument("--mixed_styles", default=None, type=str,
                        help='style folders with multiple style images')
    parser.add_argument("--stl_idx", nargs='*', default=[0],
                        help='style index')
    parser.add_argument("--render_video", action='store_true', default=False,
                        help='')
    parser.add_argument("--linear_eval", action='store_true', default=False,
                        help='')
    parser.add_argument("--offset_mlp", action='store_true', default=False,
                        help='')
    parser.add_argument("--embed_mlp", action='store_true', default=False,
                        help='')
    parser.add_argument("--rand_style", action='store_true', default=False,
                        help='')
    parser.add_argument("--patch_stride", type=int, default=1,
                        help='')
    parser.add_argument("--embed_posembed", action='store_true', default=False,
                        help='')
    parser.add_argument("--eval_on_train", action='store_true', default=False,
                        help='')
    parser.add_argument("--self_distilled", action='store_true', default=False,
                        help='self distilled geometry loss')
    parser.add_argument("--base_net_lr_rate", type=float, default=0.1,
                    help='the relative learning rate ratio of base network')
    parser.add_argument("--fast_mode", action='store_true', default=False,
                        help='only eval first image')
    parser.add_argument("--only_update_rgb", action='store_true', default=False,
                        help='only update rgb branch')
    parser.add_argument("--scale_ps_step", type=int, default=-1,
                        help='scale ps by 1/2 using given steps')

    parser.add_argument('--is_dynamic', action='store_true', default=False,
                        help='Turn on Dynamic NERF processing.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--num_voxels', type=int, default=100**3,
                    help='Number of voxels to store.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--num_voxels_base', type=int, default=100**3,
                        help='Rescales voxel sizes.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--num_voxel_grids', type=int, default=6,
                        help='Number of Voxel Grids to create and update.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--pg_scale', type=int, action="store", default=[], nargs = "*",
                        help='Progress Scaling for TiNuVox to reduce train time.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--multires_times', type=int, default=8,
                        help='Output dimension of the time embedding.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--multires_grid', type=int, default=2,
                        help='Output dimension of the voxel multi-interpolated embedding.  Only for Dynamic NeRF Datasets')
    parser.add_argument('--deformation_depth', type=int, default=3,
                        help='Depth of the deformation network.  Only for Dynamic NeRF Datasets')

    return parser


def main(args):

    device = torch.device(f'cuda:{args.gpuid}' if torch.cuda.is_available() else 'cpu')
    args.stl_idx = [float(x) for x in args.stl_idx]

    if args.patch_stride > 1:
        print(f"[Rescale]: rescale patch size from {args.patch_size} to {args.patch_size * args.patch_stride}")
        args.patch_size = args.patch_size * args.patch_stride

    # Create log dir and copy the config file
    run_dir = os.path.join(args.basedir, args.expname)
    ckpt_dir = os.path.join(run_dir, 'checkpoints')
    log_dir = os.path.join(run_dir, 'tensorboard')

    # print important info
    print(f"[Weights]: style: {args.style_weight}, content: {args.content_weight}, rgb: {args.rgb_weight}, density: {args.d_weight}")
    # Save/reload config
    if not os.path.exists(run_dir):
        if not args.eval:
            os.makedirs(run_dir)
            os.makedirs(ckpt_dir)
            os.makedirs(log_dir)

            # Dump training configuration
            config_path = os.path.join(run_dir, 'args.txt')
            parser.write_config_file(args, [config_path])
            # Backup the default config file for checking
            shutil.copy(args.config, os.path.join(run_dir, 'config.txt'))
        else:
            print("Error: The specified working directory does not exists!")
            return

    # Create dataset
    print("Loading nerf data:", args.data_path)
    train_set = PatchNeRFDataset(args.data_path, subsample=args.subsample, split='train', cam_id=False,
                            patch_size=args.patch_size, style_path=args.style_path, with_mask=args.with_mask,
                            rand_style=args.rand_style, sphere_style=args.sphere_style, mixed_styles=args.mixed_styles, patch_stride=args.patch_stride, is_dynamic=args.is_dynamic)
    test_set = PatchNeRFDataset(args.data_path, subsample=args.subsample, split='test', cam_id=False, is_dynamic=args.is_dynamic)
    try:
        exhibit_set = ExhibitNeRFDataset(args.data_path, subsample=args.subsample, is_dynamic=args.is_dynamic)
    except FileNotFoundError:
        exhibit_set = None
        print("Warning: No exhibit set!")
    # Create model and optimizer
    stl_num = get_stl_num(f"{BASE_DIR}/{args.mixed_styles}")
    xyz_min, xyz_max = None, None
    num_voxels = 0
    if(args.is_dynamic):
        xyz_min, xyz_max = compute_bbox_by_cam_frustrm(train_set.rays, *train_set.near_far())
        if(args.pg_scale):
            num_voxels = args.num_voxels // (2 ** len(args.pg_scale))
        else:
            num_voxels = args.num_voxels

    model = NeRFNet(netdepth=args.netdepth, netwidth=args.netwidth, netwidth_fine=args.netwidth_fine, netdepth_fine=args.netdepth_fine, no_skip=args.no_skip,
        act_fn=args.act_fn, N_samples=args.N_samples, N_importance=args.N_importance, viewdirs=args.use_viewdirs, use_embed=args.use_embed, multires=args.multires,
        multires_views=args.multires_views, ray_chunk=args.ray_chunk, pts_chuck=args.pts_chunk, perturb=args.perturb,
        raw_noise_std=args.raw_noise_std, fix_param=args.fix_param, zero_viewdir=args.zero_viewdir, embed_mlp=args.embed_mlp, offset_mlp=args.offset_mlp,
        embed_posembed=args.embed_posembed, stl_num=stl_num, is_dynamic=args.is_dynamic, xyz_min=xyz_min, xyz_max=xyz_max, num_voxels=num_voxels, num_voxels_base=args.num_voxels_base, num_voxel_grids=args.num_voxel_grids,
        multires_times=args.multires_times, multires_grid=args.multires_grid, deformation_depth=args.deformation_depth)
    if args.with_teach:
        teacher = NeRFNet(netdepth=args.netdepth, netwidth=args.netwidth, netwidth_fine=args.netwidth_fine, netdepth_fine=args.netdepth_fine, no_skip=args.no_skip,
            act_fn=args.act_fn, N_samples=args.N_samples, N_importance=args.N_importance, viewdirs=args.use_viewdirs, use_embed=args.use_embed, multires=args.multires,
            multires_views=args.multires_views, ray_chunk=args.ray_chunk, pts_chuck=args.pts_chunk, perturb=args.perturb,
            raw_noise_std=args.raw_noise_std, fix_param=[True, True], is_dynamic=args.is_dynamic, xyz_min=xyz_min, xyz_max=xyz_max, num_voxels=num_voxels, num_voxels_base=args.num_voxels_base, num_voxel_grids=args.num_voxel_grids,
            multires_times=args.multires_times, multires_grid=args.multires_grid, deformation_depth=args.deformation_depth)
    else:
        teacher = None
    VGG = Vgg16(requires_grad=False)

    if torch.cuda.device_count() >= 1: # TODO
        print("Multiple GPU training")
        model = nn.DataParallel(model)
        VGG = nn.DataParallel(VGG)
        if args.with_teach:
            teacher = nn.DataParallel(teacher)

    VGG, model = VGG.cuda(), model.cuda()
    if args.with_teach:
        teacher = teacher.cuda()

    optimizer = torch.optim.Adam(params=model.parameters(), lr=args.lrate, betas=(0.9, 0.999))
    scheduler = LRScheduler(optimizer=optimizer, init_lr=args.lrate, decay_rate=args.decay_rate, decay_steps=args.decay_step*1000)

    transformer = None
    # fix part of weights
    if args.only_update_rgb:
        print("[Info]: only update RGB layers")
        my_list = ['rgb_linear', 'views_linears']
        for p in model.module.nerf.mlp.named_parameters():
            p[1].requires_grad = False
            for x in my_list:
                flag = False
                if x in p[0]:
                    flag = True
                    break
            if flag:
                print(p[0])
                p[1].requires_grad = True
        for p in model.module.nerf_fine.mlp.named_parameters():
            p[1].requires_grad = False
            for x in my_list:
                flag = False
                if x in p[0]:
                    flag = True
                    break
            if flag:
                print(p[0])
                p[1].requires_grad = True

    global_step = 0
    # find and load checkpoint
    ckpt_path, ckpt_dict = args.ckpt_path, None
    if ckpt_path not in [None, 'None', '']:
        if os.path.exists(ckpt_path):
            ckpt_dict = torch.load(ckpt_path, map_location="cpu")
        else:
            raise RuntimeError("ckpt is specified but not exists")

    # reload from checkpoint
    if ckpt_dict is not None:
        print("Reloading from checkpoint:", ckpt_path)
        global_step = ckpt_dict['global_step']
        strict = False
        if args.eval:
            strict = True
        model.module.load_state_dict({k.replace('module.',''):v for k,v in ckpt_dict['model'].items()}, strict=strict)
        try:
            optimizer.load_state_dict(ckpt_dict['optimizer'])
        except:
            print("[Warning!] Optimizer load failed")
        if args.with_teach:
            teach_ckpt_path = args.teach_ckpt_path
            if not os.path.exists(teach_ckpt_path):
                teach_ckpt_path = args.ckpt_path
            ckpt_dict = torch.load(teach_ckpt_path, map_location="cpu")
            print(f"[Teach Model]: load from {teach_ckpt_path}")
            teacher.module.load_state_dict({k.replace('module.',''):v for k,v in ckpt_dict['model'].items()}, strict=True)

    ####### Training stage #######
    print(train_set[0])

    if not args.eval:
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=args.batch_size, shuffle=True, drop_last=True,
            collate_fn=Ray_Batch_Collate(), num_workers=args.num_workers, pin_memory=args.pin_mem)

        # Summary writers
        summary_writer = SummaryWriter(log_dir=log_dir)
        while global_step < args.N_iters:
            if(args.is_dynamic):
                global_step = train_one_epoch_dynamic([model, teacher, VGG, transformer], optimizer, scheduler,
                    train_loader, test_set, exhibit_set, summary_writer,
                    global_step, args.N_iters, run_dir, device=device,
                    i_print=args.i_print, i_img=args.i_img, log_img_idx=args.log_img_idx,
                    i_weights=args.i_weights, i_testset=args.i_testset, i_video=args.i_video, args=args)
            else:
                global_step = train_one_epoch([model, teacher, VGG, transformer], optimizer, scheduler,
                    train_loader, test_set, exhibit_set, summary_writer,
                    global_step, args.N_iters, run_dir, device=device,
                    i_print=args.i_print, i_img=args.i_img, log_img_idx=args.log_img_idx,
                    i_weights=args.i_weights, i_testset=args.i_testset, i_video=args.i_video, args=args)
            if global_step % args.i_weights:
                save_checkpoint(os.path.join(ckpt_dir, 'latest.ckpt'), global_step, model, optimizer)

    ############# Test stage#################
    save_dir = os.path.join(run_dir, 'eval')
    os.makedirs(save_dir, exist_ok=True)
    '''You can either use test_set or exhibit_set in rendering a video
    '''
    if args.eval:
        if args.linear_eval:
            print(f"[Eval]: Linear Eval")
            linear_eval(model, test_set, device=device, save_dir=save_dir,  expname=args.expname, stl_idx=torch.Tensor(args.stl_idx).cuda(), bs=args.batch_size)
        else:
            if args.eval_on_train:
                evaluate(model, train_set, device=device, save_dir=save_dir, stl_idx=torch.Tensor(args.stl_idx).cuda(), bs=args.batch_size, is_dynamic=args.is_dynamic)
            else:
                evaluate(model, test_set, device=device, save_dir=save_dir, stl_idx=torch.Tensor(args.stl_idx).cuda(), bs=args.batch_size, is_dynamic=args.is_dynamic)
        exit(0)

    if args.render_video:
        render_video(model, exhibit_set, device=device, save_dir=save_dir, expname=args.expname, stl_idx=torch.Tensor(args.stl_idx).cuda(), bs=args.batch_size, is_dynamic=args.is_dynamic)
        exit(0)

if __name__=='__main__':
    # Random seed
    np.random.seed(0)

    # Read arguments and configs
    parser = create_arg_parser()
    args, _ = parser.parse_known_args()

    # enable error detection
    torch.autograd.set_detect_anomaly(True)

    main(args)




