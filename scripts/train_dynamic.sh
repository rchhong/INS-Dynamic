#! /in/bash

SCENE=lego
CONFIG_NAME=lego_dynamic
BATCH_SIZE=1
# sentitive to gpu memory, lower it if gpu mem is small
PATCH_SIZE=18
# related to receptive field
PATCH_STRIDE=2
RGB_W=1
STYLE_W=1e11
CONTENT_W=1
DENSITY_W=1e6
EXPNAME=${CONFIG_NAME}_dynamic_P${PATCH_SIZE}_PS${PATCH_STRIDE}_RGB${RGB_W}_S${STYLE_W}_C${CONTENT_W}_D${DENSITY_W}_updateAll
mkdir -p logs/$CONFIG_NAME
mkdir -p logs/$CONFIG_NAME/checkpoints

CUDA_VISIBLE_DEVICES=1 python run_nerf.py \
 --batch_size ${BATCH_SIZE} \
 --config configs/${CONFIG_NAME}.txt  \
 --data_path datasets/dnerf_synthetic/${SCENE}   \
 --expname $EXPNAME    \
 --d_weight $DENSITY_W  \
 --content_weight $CONTENT_W  \
 --rgb_weight ${RGB_W}   \
 --style_weight  ${STYLE_W}  \
 --patch_size ${PATCH_SIZE}  \
 --N_iters 20000  \
 --render_video \
 --loss_terms coarse fine    \
 --fix_param False False \
 --i_testset 2500  \
 --i_video 2500  \
 --i_weights 2500 \
 --patch_stride ${PATCH_STRIDE} \
 --stl_idx 0 \
 --is_dynamic \
 --style_path datasets/single_styles/the_scream.jpg \
#  --eval
#  --with_teach  \
# --ckpt_path ckpts/${CONFIG_NAME}_00170000.ckpt 2>&1 | tee -a logs/${EXPNAME}/${EXPNAME}.txt