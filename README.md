# Stylized Dynamic NeRFs

<!-- [![License: MIT](https://img.shields.io/badge/License-MIT-green.svg)](https://opensource.org/licenses/MIT) -->
<!-- [[Paper]](https://arxiv.org/abs/2204.01943) [[Website]](https://zhiwenfan.github.io/INS/) -->

<div>
<img src="https://raw.githubusercontent.com/rchhong/INS-Dynamic/blob/master/gifs/bouncingballs_starry.gif"/>
<img src="https://raw.githubusercontent.com/rchhong/INS-Dynamic/blob/master/gifs/lego_scream.gif"/>
</div>

Stylized Dynamic NeRFs provide a framework for stylizing dynamic NeRFs. Dynamic NeRFs are created using the [TiNeuVox](https://github.com/hustvl/TiNeuVox) model. Stylization is then performed using the [Implicit Neural Stylization](https://github.com/VITA-Group/INS) pipeline.

## Installation

We recommend users to use `conda` to install the running environment. The following dependencies are required:

```
pytorch=1.7.0
torchvision=0.8.0
cudatoolkit=11.0
tensorboard=2.7.0
opencv
imageio
imageio-ffmpeg
configargparse
scipy
matplotlib
tqdm
mrc
lpips
```

## Data Preparation

To run our code on NeRF dataset, users need first download data from the [DNeRF dropbox](https://drive.google.com/drive/folders/128yBriW1IG_3NJ5Rp7APSTZsJqdJdfc1). Then extract package files according to the following directory structure:

```
├── configs
│   ├── ...
│
├── datasets
|   ├── dnerf_synthetic
|   |   └── bouncingballs
|   |   └── hellwarrior    # downloaded synthetic dataset
|   |   └── ...
```

The last step is to generate and process data via our provided script:

```
python gen_dataset.py --config <config_file>
```

where `<config_file>` is the path to the configuration file of your experiment instance. Examples and pre-defined configuration files are provided in `configs` folder. Note that dynamic configs end in `_dynamic.txt`.

## Training

After generating datasets, one can train the base dynamic NeRF using:

```
bash scripts/train_dynamic.sh
```

and stylize the base dynamic NeRF:

```
bash scripts/train_dynamic_with_teacher.sh
```

Ensure that `ckpt_path` points to the correct checkpoint of the teacher model.

## Testing

To evaluate models, enable the `eval` flag on one of the above scripts.
