import torch
import numpy as np
import logging
import torch.multiprocessing as mp
import torch.distributed as dist
import random
import os
import subprocess


def get_voxel_centers(voxel_coords, downsample_times, voxel_size, point_cloud_range):
    """
    Args:
        voxel_coords: (N, 3)
        downsample_times:
        voxel_size:
        point_cloud_range:

    Returns:

    """
    assert voxel_coords.shape[1] == 3
    voxel_centers = voxel_coords[:, [2, 1, 0]].float()  # (xyz)
    voxel_size = torch.tensor(voxel_size, device=voxel_centers.device).float() * downsample_times
    pc_range = torch.tensor(point_cloud_range[0:3], device=voxel_centers.device).float()
    voxel_centers = (voxel_centers + 0.5) * voxel_size + pc_range
    return voxel_centers

def check_numpy_to_torch(x):
    if isinstance(x, np.ndarray):
        return torch.from_numpy(x).float(), True
    return x, False

def rotate_pc_along_z(pc, rot_angle):
    """
    params pc: (N, 3+C), (N, 3) is in the LiDAR coordinate
    params rot_angle: rad scalar
    Output pc: updated pc with XYZ rotated
    """
    cosval = np.cos(rot_angle)
    sinval = np.sin(rot_angle)
    rotmat = np.array([[cosval, -sinval], [sinval, cosval]])
    pc[:, 0:2] = np.dot(pc[:, 0:2], rotmat)
    return pc


def rotate_pc_along_z_torch(pc, rot_angle, inplace=True):
    """
    :param pc: (N, num_points, 3 + C) in the LiDAR coordinate
    :param rot_angle: (N)
    :return:
    """
    cosa = torch.cos(rot_angle).view(-1, 1)  # (N, 1)
    sina = torch.sin(rot_angle).view(-1, 1)  # (N, 1)

    raw_1 = torch.cat([cosa, -sina], dim=1)  # (N, 2)
    raw_2 = torch.cat([sina, cosa], dim=1)  # (N, 2)
    R = torch.cat((raw_1.unsqueeze(dim=1), raw_2.unsqueeze(dim=1)), dim=1)  # (N, 2, 2)

    pc_temp = pc[:, :, 0:2]  # (N, 512, 2)

    if inplace:
        pc[:, :, 0:2] = torch.matmul(pc_temp, R)  # (N, 512, 2)
    else:
        xy_rotated = torch.matmul(pc_temp, R)  # (N, 512, 2)
        pc = torch.cat((xy_rotated, pc[:, :, 2:]), dim=2)
    return pc

def rotate_points_along_z(points, angle):
    """
    Args:
        points: (B, N, 3 + C)
        angle: (B), angle along z-axis, angle increases x ==> y
    Returns:

    """
    points, is_numpy = check_numpy_to_torch(points)
    angle, _ = check_numpy_to_torch(angle)

    cosa = torch.cos(angle)
    sina = torch.sin(angle)
    zeros = angle.new_zeros(points.shape[0])
    ones = angle.new_ones(points.shape[0])
    rot_matrix = torch.stack((
        cosa,  sina, zeros,
        -sina, cosa, zeros,
        zeros, zeros, ones
    ), dim=1).view(-1, 3, 3).float()
    points_rot = torch.matmul(points[:, :, 0:3], rot_matrix)
    points_rot = torch.cat((points_rot, points[:, :, 3:]), dim=-1)
    return points_rot.numpy() if is_numpy else points_rot

def mask_points_by_range(points, limit_range):
    mask = (points[:, 0] >= limit_range[0]) & (points[:, 0] <= limit_range[3]) \
           & (points[:, 1] >= limit_range[1]) & (points[:, 1] <= limit_range[4])
    points = points[mask]
    return points

def mask_repeat_points(points):
    indices = np.unique(points,axis=0)
    return indices


def enlarge_box3d(boxes3d, extra_width):
    """
    :param boxes3d: (N, 7) [x, y, z, w, l, h, ry] in LiDAR coords
    """
    if isinstance(boxes3d, np.ndarray):
        large_boxes3d = boxes3d.copy()
    else:
        large_boxes3d = boxes3d.clone()
    large_boxes3d[:, 3:6] += extra_width * 2
    large_boxes3d[:, 2] -= extra_width  # bugfixed: here should be minus, not add in LiDAR, 20190508
    return large_boxes3d


def drop_info_with_name(info, name):
    ret_info = {}
    keep_indices = [i for i, x in enumerate(info['name']) if x != name]
    for key in info.keys():
        # if key in ['truncated','occluded','alpha','bbox','score','difficulty']:
        #     continue
        ret_info[key] = info[key][keep_indices]
    return ret_info


def drop_arrays_by_name(gt_names, used_classes):
    inds = [i for i, x in enumerate(gt_names) if x not in used_classes]
    inds = np.array(inds, dtype=np.int64)
    return inds


def keep_arrays_by_name(gt_names, used_classes):
    inds = [i for i, x in enumerate(gt_names) if x in used_classes]
    inds = np.array(inds, dtype=np.int64)
    return inds


def limit_period_v0(val, offset=0.5, period=np.pi):
    return val - np.floor(val / period + offset) * period


def limit_period(val, offset=0.5, period=np.pi):
    val, is_numpy = check_numpy_to_torch(val)
    ans = val - torch.floor(val / period + offset) * period
    return ans.numpy() if is_numpy else ans


def dict_select(dict_src, inds):
    for key, val in dict_src.items():
        if isinstance(val, dict):
            dict_select(val, inds)
        else:
            dict_src[key] = val[inds]


def create_logger(log_file, rank=0, log_level=logging.INFO):
    logger = logging.getLogger(__name__)
    logger.setLevel(log_level if rank == 0 else 'ERROR')
    formatter = logging.Formatter('%(asctime)s  %(levelname)5s  %(message)s')
    console = logging.StreamHandler()
    console.setLevel(log_level if rank == 0 else 'ERROR')
    console.setFormatter(formatter)
    file_handler = logging.FileHandler(filename=log_file)
    file_handler.setLevel(log_level if rank == 0 else 'ERROR')
    file_handler.setFormatter(formatter)
    logger.addHandler(console)
    logger.addHandler(file_handler)
    return logger


def init_dist_pytorch(batch_size, tcp_port, local_rank, backend='nccl'):
    if mp.get_start_method(allow_none=True) is None:
        mp.set_start_method('spawn')
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(local_rank % num_gpus)
    dist.init_process_group(
        backend=backend,
        rank=local_rank,
        # init_method='tcp://127.0.0.1:%d' % tcp_port,
        init_method='tcp://10.141.77.234:%d' % tcp_port,
        world_size=num_gpus,
    )
    print("num_gpus",num_gpus)
    assert batch_size % num_gpus == 0, 'Batch size should be matched with GPUS: (%d, %d)' % (batch_size, num_gpus)
    batch_size_each_gpu = batch_size // num_gpus
    rank = dist.get_rank()
    print("bs_per_gpu", batch_size_each_gpu)
    return batch_size_each_gpu, rank


def init_dist_slurm(batch_size, tcp_port, local_rank=None, backend='nccl'):
    """
    modified from https://github.com/open-mmlab/mmdetection
    :param batch_size:
    :param tcp_port:
    :param local_rank:
    :param backend:
    :return:
    """
    proc_id = int(os.environ['SLURM_PROCID'])
    ntasks = int(os.environ['SLURM_NTASKS'])
    node_list = os.environ['SLURM_NODELIST']
    num_gpus = torch.cuda.device_count()
    torch.cuda.set_device(proc_id % num_gpus)
    addr = subprocess.getoutput('scontrol show hostname {} | head -n1'.format(node_list))
    os.environ['MASTER_PORT'] = str(tcp_port)
    os.environ['MASTER_ADDR'] = addr
    os.environ['WORLD_SIZE'] = str(ntasks)
    os.environ['RANK'] = str(proc_id)
    dist.init_process_group(backend=backend)

    total_gpus = dist.get_world_size()
    assert batch_size % total_gpus == 0, 'Batch size should be matched with GPUS: (%d, %d)' % (batch_size, total_gpus)
    batch_size_each_gpu = batch_size // total_gpus
    rank = dist.get_rank()
    return batch_size_each_gpu, rank


def set_random_seed(seed):
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
