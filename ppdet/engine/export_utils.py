# Copyright (c) 2020 PaddlePaddle Authors. All Rights Reserved. 
#   
# Licensed under the Apache License, Version 2.0 (the "License");   
# you may not use this file except in compliance with the License.  
# You may obtain a copy of the License at   
#   
#     http://www.apache.org/licenses/LICENSE-2.0    
#   
# Unless required by applicable law or agreed to in writing, software   
# distributed under the License is distributed on an "AS IS" BASIS, 
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.  
# See the License for the specific language governing permissions and   
# limitations under the License.

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import yaml
from collections import OrderedDict

import paddle
from ppdet.data.source.category import get_categories

from ppdet.utils.logger import setup_logger
logger = setup_logger('ppdet.engine')

# Global dictionary
TRT_MIN_SUBGRAPH = {
    'YOLO': 3,
    'PPYOLOE': 10,
    'YOLOX': 20,
    'YOLOv5': 20,
    'RTMDet': 20,
    'YOLOv6': 10,
    'YOLOv7': 10,
    'YOLOv8': 10,
}

TO_STATIC_SPEC = {
    'yolov3_darknet53_270e_coco': [{
        'im_id': paddle.static.InputSpec(
            name='im_id', shape=[-1, 1], dtype='float32'),
        'is_crowd': paddle.static.InputSpec(
            name='is_crowd', shape=[-1, 50], dtype='float32'),
        'gt_bbox': paddle.static.InputSpec(
            name='gt_bbox', shape=[-1, 50, 4], dtype='float32'),
        'curr_iter': paddle.static.InputSpec(
            name='curr_iter', shape=[-1], dtype='float32'),
        'image': paddle.static.InputSpec(
            name='image', shape=[-1, 3, -1, -1], dtype='float32'),
        'im_shape': paddle.static.InputSpec(
            name='im_shape', shape=[-1, 2], dtype='float32'),
        'scale_factor': paddle.static.InputSpec(
            name='scale_factor', shape=[-1, 2], dtype='float32'),
        'target0': paddle.static.InputSpec(
            name='target0', shape=[-1, 3, 86, -1, -1], dtype='float32'),
        'target1': paddle.static.InputSpec(
            name='target1', shape=[-1, 3, 86, -1, -1], dtype='float32'),
        'target2': paddle.static.InputSpec(
            name='target2', shape=[-1, 3, 86, -1, -1], dtype='float32'),
    }],
}


def apply_to_static(config, model):
    filename = config.get('filename', None)
    spec = TO_STATIC_SPEC.get(filename, None)
    model = paddle.jit.to_static(model, input_spec=spec)
    logger.info("Successfully to apply @to_static with specs: {}".format(spec))
    return model


def _prune_input_spec(input_spec, program, targets):
    # try to prune static program to figure out pruned input spec
    # so we perform following operations in static mode
    device = paddle.get_device()
    paddle.enable_static()
    paddle.set_device(device)
    pruned_input_spec = [{}]
    program = program.clone()
    program = program._prune(targets=targets)
    global_block = program.global_block()
    for name, spec in input_spec[0].items():
        try:
            v = global_block.var(name)
            pruned_input_spec[0][name] = spec
        except Exception:
            pass
    paddle.disable_static(place=device)
    return pruned_input_spec


def _parse_reader(reader_cfg, dataset_cfg, metric, arch, image_shape):
    preprocess_list = []

    anno_file = dataset_cfg.get_anno()

    clsid2catid, catid2name = get_categories(metric, anno_file, arch)

    label_list = [str(cat) for cat in catid2name.values()]

    fuse_normalize = reader_cfg.get('fuse_normalize', False)
    sample_transforms = reader_cfg['sample_transforms']
    for st in sample_transforms[1:]:
        for key, value in st.items():
            p = {'type': key}
            if key == 'Resize':
                if int(image_shape[1]) != -1:
                    value['target_size'] = image_shape[1:]
                value['interp'] = value.get('interp', 1)  # cv2.INTER_LINEAR
            if fuse_normalize and key == 'NormalizeImage':
                continue
            p.update(value)
            preprocess_list.append(p)
    batch_transforms = reader_cfg.get('batch_transforms', None)
    if batch_transforms:
        for bt in batch_transforms:
            for key, value in bt.items():
                # for deploy/infer, use PadStride(stride) instead PadBatch(pad_to_stride)
                if key == 'PadBatch':
                    preprocess_list.append({
                        'type': 'PadStride',
                        'stride': value['pad_to_stride']
                    })
                    break

    return preprocess_list, label_list


def _parse_tracker(tracker_cfg):
    tracker_params = {}
    for k, v in tracker_cfg.items():
        tracker_params.update({k: v})
    return tracker_params


def _dump_infer_config(config, path, image_shape, model):
    arch_state = False
    from ppdet.core.config.yaml_helpers import setup_orderdict
    setup_orderdict()
    use_dynamic_shape = True if image_shape[2] == -1 else False
    infer_cfg = OrderedDict({
        'mode': 'paddle',
        'draw_threshold': 0.5,
        'metric': config['metric'],
        'use_dynamic_shape': use_dynamic_shape
    })
    export_onnx = config.get('export_onnx', False)
    export_eb = config.get('export_eb', False)

    infer_arch = config['architecture']
    if 'RCNN' in infer_arch and export_onnx:
        logger.warning(
            "Exporting RCNN model to ONNX only support batch_size = 1")
        infer_cfg['export_onnx'] = True
        infer_cfg['export_eb'] = export_eb

    for arch, min_subgraph_size in TRT_MIN_SUBGRAPH.items():
        if arch in infer_arch:
            infer_cfg['arch'] = arch
            infer_cfg['min_subgraph_size'] = min_subgraph_size
            arch_state = True
            break

    if infer_arch in [
            'YOLOX', 'PPYOLOE', 'YOLOv5', 'YOLOv6', 'YOLOv7', 'YOLOv8'
    ]:
        infer_cfg['arch'] = infer_arch
        infer_cfg['min_subgraph_size'] = TRT_MIN_SUBGRAPH[infer_arch]
        arch_state = True

    if not arch_state:
        logger.error(
            'Architecture: {} is not supported for exporting model now.\n'.
            format(infer_arch) +
            'Please set TRT_MIN_SUBGRAPH in ppdet/engine/export_utils.py')
        os._exit(0)
    if 'mask_head' in config[config['architecture']] and config[config[
            'architecture']]['mask_head']:
        infer_cfg['mask'] = True
    label_arch = 'detection_arch'

    reader_cfg = config['TestReader']
    dataset_cfg = config['TestDataset']

    infer_cfg['Preprocess'], infer_cfg['label_list'] = _parse_reader(
        reader_cfg, dataset_cfg, config['metric'], label_arch, image_shape[1:])

    yaml.dump(infer_cfg, open(path, 'w'))
    logger.info("Export inference config file to {}".format(os.path.join(path)))
