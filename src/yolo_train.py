"""Train YOLOv3 with random shapes."""

# TODO: This is a training script only, implement model save, model_fn, predict_fn

# Built-Ins:
import argparse
import json
import logging
import os
import subprocess
import time
import warnings

# Install/Update GluonCV:
subprocess.call([sys.executable, '-m', 'pip', 'install', '--upgrade', 'gluoncv'])

# External Dependencies:
import gluoncv as gcv
from gluoncv import data as gdata
from gluoncv import utils as gutils
from gluoncv.data.batchify import Tuple, Stack, Pad
from gluoncv.data.dataloader import RandomTransformDataLoader
from gluoncv.data.transforms.presets.yolo import YOLO3DefaultTrainTransform
from gluoncv.data.transforms.presets.yolo import YOLO3DefaultValTransform
from gluoncv.model_zoo import get_model
from gluoncv.utils.metrics.coco_detection import COCODetectionMetric
from gluoncv.utils import LRScheduler, LRSequential
from hello import VOC07MApMetric
import mxnet as mx
from mxnet import nd
from mxnet import gluon
from mxnet import autograd
import numpy as np

    
def parse_args():
    parser = argparse.ArgumentParser(description='Train YOLO networks with random input shape.')
    parser.add_argument('--network', type=str, default='yolo3_darknet53_coco',
                        help="Base network name which serves as feature extraction base.")
    parser.add_argument('--data-shape', type=int, default=320,
                        help="Input data shape for evaluation, use 320, 416, 608... " +
                             "Training is with random shapes from (320 to 608).")
    parser.add_argument('--batch-size', type=int, default=4, help='Training mini-batch size')
    parser.add_argument('--num-workers', '-j', dest='num_workers', type=int,
                        default=0, help='Number of data workers, you can use larger '
                        'number to accelerate data loading, if you CPU and GPUs are powerful.')
    parser.add_argument('--num-gpus', type=int, default=os.environ['SM_NUM_GPUS'],
                        help='Number of GPUs to use in training.')
    parser.add_argument('--epochs', type=int, default=1,
                        help='Training epochs.')
    parser.add_argument('--resume', type=str, default='',
                        help='Resume from previously saved parameters if not None. '
                        'For example, you can resume from ./yolo3_xxx_0123.params')
    parser.add_argument('--start-epoch', type=int, default=0,
                        help='Starting epoch for resuming, default is 0 for new training.'
                        'You can specify it to 100 for example to start from 100 epoch.')
    parser.add_argument('--lr', type=float, default=0.001,
                        help='Learning rate, default is 0.001')
    parser.add_argument('--optimizer', type=str, default='sgd',
                        help='Optimizer used for training, default is sgd')
    parser.add_argument('--lr-mode', type=str, default='step',
                        help='learning rate scheduler mode. options are step, poly and cosine.')
    parser.add_argument('--lr-decay', type=float, default=0.1,
                        help='decay rate of learning rate. default is 0.1.')
    parser.add_argument('--lr-decay-period', type=int, default=0,
                        help='interval for periodic learning rate decays. default is 0 to disable.')
    parser.add_argument('--lr-decay-epoch', type=str, default='160,180',
                        help='epochs at which learning rate decays. default is 160,180.')
    parser.add_argument('--warmup-lr', type=float, default=0.0,
                        help='starting warmup learning rate. default is 0.0.')
    parser.add_argument('--warmup-epochs', type=int, default=0,
                        help='number of warmup epochs.')
    parser.add_argument('--momentum', type=float, default=0.9,
                        help='SGD momentum, default is 0.9')
    parser.add_argument('--wd', type=float, default=0.0005,
                        help='Weight decay, default is 5e-4')
    parser.add_argument('--log-interval', type=int, default=100,
                        help='Logging mini-batch interval. Default is 100.')
    parser.add_argument('--save-prefix', type=str, default='',
                        help='Saving parameter prefix')
    parser.add_argument('--save-interval', type=int, default=10,
                        help='Saving parameters epoch interval, best model will always be saved.')
    parser.add_argument('--val-interval', type=int, default=1,
                        help='Epoch interval for validation, increase the number will reduce the '
                             'training time if validation is slow.')
    parser.add_argument('--seed', type=int, default=233,
                        help='Random seed to be fixed.')
    parser.add_argument('--num-samples', type=int, default=-1,
                        help='Training images. Use -1 to automatically get the number.')
    parser.add_argument('--syncbn', action='store_true',
                        help='Use synchronize BN across devices.')
    parser.add_argument('--no-random-shape', action='store_true',
                        help='Use fixed size(data-shape) throughout the training, which will be faster '
                        'and require less memory. However, final model will be slightly worse.')
    parser.add_argument('--no-wd', action='store_true',
                        help='whether to remove weight decay on bias, and beta/gamma for batchnorm layers.')
    parser.add_argument('--mixup', action='store_true',
                        help='whether to enable mixup.')
    parser.add_argument('--no-mixup-epochs', type=int, default=20,
                        help='Disable mixup training if enabled in the last N epochs.')
    parser.add_argument('--pretrained', action='store_false',
                        help='Use pretrained weights')

    
    # Data, model, and output directories
    parser.add_argument('--output-data-dir', type=str, default=os.environ['SM_OUTPUT_DATA_DIR'])
    parser.add_argument('--model-dir', type=str, default=os.environ['SM_MODEL_DIR'])
    parser.add_argument('--train', type=str, default=os.environ['SM_CHANNEL_TRAIN'])
    parser.add_argument('--test', type=str, default=os.environ['SM_CHANNEL_TEST'])
    parser.add_argument('--images', type=str, default=os.environ['SM_CHANNEL_IMAGES'])
    
    parser.add_argument('--label-smooth', action='store_true', help='Use label smoothing.')
    args = parser.parse_args()
    return args

class GroundTruthDataset(gluon.data.Dataset):
    """
    Custom Dataset to handle the GroundTruth json file
    """
    def __init__(self, data_path, channel, image_path, field_name):
        """
        Parameters
        ---------
        data_path: str, Path to the data folder, default 'data'
        field_name: str, The annotation task name that appears in your json
                    the parent node of `annotations` that holds bbs infos

        """
        self.data_path = data_path
        self.image_path= image_path
        self.field_name = field_name
        self.image_info = []
        with open(os.path.join(data_path, '{}.manifest'.format(channel))) as f:
            lines = f.readlines()
            for line in lines:
                info = json.loads(line[:-1])
                if len(info[field_name]['annotations']):
                    self.image_info.append(info)

    def __getitem__(self, idx):
        """
        Parameters
        ---------
        idx: int, index requested

        Returns
        -------
        image: nd.NDArray
            The image 
        label: np.NDArray bounding box labels of the form [[x1,y1, x2, y2, class], ...]
        """
        info = self.image_info[idx]
        source_ref = (
            info['source-ref'][5:].partition('/')[2]
            if (info['source-ref'][:5] == 's3://')
            else info['source-ref']
        )
        image = mx.image.imread(os.path.join(self.image_path,*source_ref.split('/')))
        boxes = info[self.field_name]['annotations']
        label = []
        for box in boxes:
            label.append([box['left'], box['top'], 
                box['left']+box['width'], box['top']+box['height'], 0])

        return image, np.array(label)

    def __len__(self):
        return len(self.image_info)

def get_dataset(args): 
    print(os.listdir('/opt/ml/input/data/'))
    print(os.listdir('/opt/ml/input/data/train/'))
    train_dataset = GroundTruthDataset(args.train,'train',args.images,"labels")
    val_dataset = GroundTruthDataset(args.test,'validation',args.images,"labels")
    
    val_metric = VOC07MApMetric(iou_thresh=0.5)
    
    if args.num_samples < 0:
        args.num_samples = len(train_dataset)
    if args.mixup:
        from gluoncv.data import MixupDetection
        train_dataset = MixupDetection(train_dataset)
    return train_dataset, val_dataset, val_metric

def get_dataloader(net, train_dataset, val_dataset, data_shape, batch_size, num_workers, args):
    """Get dataloader."""
    width, height = data_shape, data_shape
    batchify_fn = Tuple(*([Stack() for _ in range(6)] + [Pad(axis=0, pad_val=-1) for _ in range(1)]))  # stack image, all targets generated
    if args.no_random_shape:
        print("no random shape")
        train_loader = gluon.data.DataLoader(
            train_dataset.transform(YOLO3DefaultTrainTransform(width, height, net, mixup=args.mixup)),
            batch_size, True, batchify_fn=batchify_fn, last_batch='discard', num_workers=num_workers)
    else:
        print("with random shape")
        transform_fns = [YOLO3DefaultTrainTransform(x * 32, x * 32, net, mixup=args.mixup) for x in range(10, 20)]
        train_loader = RandomTransformDataLoader(
            transform_fns, train_dataset, batch_size=batch_size, interval=10, last_batch='discard',
            shuffle=True, batchify_fn=batchify_fn, num_workers=num_workers)
    val_batchify_fn = Tuple(Stack(), Pad(pad_val=-1))    
    val_loader = gluon.data.DataLoader(
        val_dataset.transform(YOLO3DefaultValTransform(width, height)),
        batch_size, True, batchify_fn=val_batchify_fn, last_batch='discard', num_workers=num_workers)
    return train_loader, val_loader

def save_params(net, best_map, current_map, epoch, save_interval, prefix):
    current_map = float(current_map)
    if current_map > best_map[0]:
        best_map[0] = current_map
        net.save_parameters('{:s}_best.params'.format(prefix, epoch, current_map))
        with open(prefix+'_best_map.log', 'a') as f:
            f.write('{:04d}:\t{:.4f}\n'.format(epoch, current_map))
    if save_interval and epoch % save_interval == 0:
        net.save_parameters('{:s}_{:04d}_{:.4f}.params'.format(prefix, epoch, current_map))

def validate(net, val_data, ctx, eval_metric):
    """Test on validation dataset."""
    eval_metric.reset()
    # set nms threshold and topk constraint
    net.set_nms(nms_thresh=0.45, nms_topk=400)
    mx.nd.waitall()
    net.hybridize()
  
    for batch in val_data:
        data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
        label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)
        det_bboxes = []
        det_ids = []
        det_scores = []
        gt_bboxes = []
        gt_ids = []        
        for x, y in zip(data, label):
            # get prediction results
            ids, scores, bboxes = net(x)
            det_ids.append(ids)
            det_scores.append(scores)
            # clip to image size
            det_bboxes.append(bboxes.clip(0, batch[0].shape[2]))
            # split ground truths
            gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
            gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))

        # update metric        
        eval_metric.update(det_bboxes, det_ids, det_scores, gt_bboxes, gt_ids)
    return eval_metric.get()

def validate_train(net, train_data, ctx, eval_metric):
    """Test on validation dataset."""
    clipper = gcv.nn.bbox.BBoxClipToImage()
    eval_metric.reset()
    if not args.disable_hybridization:
        # input format is differnet than training, thus rehybridization is needed.
        net.hybridize(static_alloc=args.static_alloc)
    for batch in val_data:
        data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
        label = gluon.utils.split_and_load(batch[1], ctx_list=ctx, batch_axis=0, even_split=False)
        det_bboxes = []
        det_ids = []
        det_scores = []
        gt_bboxes = []
        gt_ids = []        
        for x, y in zip(data, label):
            # get prediction results
            ids, scores, bboxes = net(x)
            det_ids.append(ids)
            det_scores.append(scores)
            # clip to image size
            det_bboxes.append(bboxes.clip(0, batch[0].shape[2]))
            # split ground truths
            gt_ids.append(y.slice_axis(axis=-1, begin=4, end=5))
            gt_bboxes.append(y.slice_axis(axis=-1, begin=0, end=4))

        # update metric
        for det_bbox, det_id, det_score, gt_bbox, gt_id, gt_diff in zip(det_bboxes, det_ids,
                                                                        det_scores, gt_bboxes,
                                                                        gt_ids, gt_difficults):
            eval_metric.update(det_bbox, det_id, det_score, gt_bbox, gt_id, gt_diff)

def train(net, train_data, val_data, eval_metric, ctx, args):
    """Training pipeline"""
    net.collect_params().reset_ctx(ctx)
    if args.no_wd:
        for k, v in net.collect_params('.*beta|.*gamma|.*bias').items():
            v.wd_mult = 0.0

    if args.label_smooth:
        net._target_generator._label_smooth = True

    if args.lr_decay_period > 0:
        lr_decay_epoch = list(range(args.lr_decay_period, args.epochs, args.lr_decay_period))
    else:
        lr_decay_epoch = [int(i) for i in args.lr_decay_epoch.split(',')]
    
    lr_scheduler = LRSequential([
        LRScheduler('linear', base_lr=0, target_lr=args.lr,
                    nepochs=args.warmup_epochs, iters_per_epoch=args.batch_size),
        LRScheduler(args.lr_mode, base_lr=args.lr,
                    nepochs=args.epochs - args.warmup_epochs,
                    iters_per_epoch=args.batch_size,
                    step_epoch=lr_decay_epoch,
                    step_factor=args.lr_decay, power=2),
    ])
    if args.optimizer =='sgd':
        trainer = gluon.Trainer(
            net.collect_params(), args.optimizer,
            {'wd': args.wd, 'momentum': args.momentum, 'lr_scheduler': lr_scheduler},
            kvstore='local')
    elif args.optimizer =='adam':
        trainer = gluon.Trainer(
            net.collect_params(), args.optimizer,
            {'lr_scheduler': lr_scheduler},kvstore='local')
    else:
        trainer = gluon.Trainer(
            net.collect_params(), args.optimizer, kvstore='local')

    # targets
    sigmoid_ce = gluon.loss.SigmoidBinaryCrossEntropyLoss(from_sigmoid=False)
    l1_loss = gluon.loss.L1Loss()

    # metrics
    obj_metrics = mx.metric.Loss('ObjLoss')
    center_metrics = mx.metric.Loss('BoxCenterLoss')
    scale_metrics = mx.metric.Loss('BoxScaleLoss')
    cls_metrics = mx.metric.Loss('ClassLoss')

    # set up logger
    logging.basicConfig()
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    log_file_path = args.save_prefix + '_train.log'
    log_dir = os.path.dirname(log_file_path)
    if log_dir and not os.path.exists(log_dir):
        os.makedirs(log_dir)
    fh = logging.FileHandler(log_file_path)
    logger.addHandler(fh)
    logger.info(args)
    logger.info('Start training from [Epoch {}]'.format(args.start_epoch))
    best_map = [0]
    for epoch in range(args.start_epoch, args.epochs):
        if args.mixup:
            # TODO(zhreshold): more elegant way to control mixup during runtime
            try:
                train_data._dataset.set_mixup(np.random.beta, 1.5, 1.5)
            except AttributeError:
                train_data._dataset._data.set_mixup(np.random.beta, 1.5, 1.5)
            if epoch >= args.epochs - args.no_mixup_epochs:
                try:
                    train_data._dataset.set_mixup(None)
                except AttributeError:
                    train_data._dataset._data.set_mixup(None)

        tic = time.time()
        btic = time.time()
        mx.nd.waitall()
        net.hybridize()
        for i, batch in enumerate(train_data):
            print("epoch ", epoch, ", batch ", i)
            batch_size = batch[0].shape[0]
            data = gluon.utils.split_and_load(batch[0], ctx_list=ctx, batch_axis=0, even_split=False)
            # objectness, center_targets, scale_targets, weights, class_targets
            fixed_targets = [gluon.utils.split_and_load(batch[it], ctx_list=ctx, batch_axis=0, even_split=False) for it in range(1, 6)]
            gt_boxes = gluon.utils.split_and_load(batch[6], ctx_list=ctx, batch_axis=0, even_split=False)
            sum_losses = []
            obj_losses = []
            center_losses = []
            scale_losses = []
            cls_losses = []
            with autograd.record():
                for ix, x in enumerate(data):
                    obj_loss, center_loss, scale_loss, cls_loss = net(x, gt_boxes[ix], *[ft[ix] for ft in fixed_targets])
                    sum_losses.append(obj_loss + center_loss + scale_loss + cls_loss)
                    obj_losses.append(obj_loss)
                    center_losses.append(center_loss)
                    scale_losses.append(scale_loss)
                    cls_losses.append(cls_loss)
                autograd.backward(sum_losses)            
            trainer.step(batch_size)
            obj_metrics.update(0, obj_losses)
            center_metrics.update(0, center_losses)
            scale_metrics.update(0, scale_losses)
            cls_metrics.update(0, cls_losses)
            if args.log_interval and not (i + 1) % args.log_interval:
                name1, loss1 = obj_metrics.get()
                name2, loss2 = center_metrics.get()
                name3, loss3 = scale_metrics.get()
                name4, loss4 = cls_metrics.get()
                logger.info('[Epoch {}][Batch {}], LR: {:.2E}, Speed: {:.3f} samples/sec, {}={:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
                    epoch, i, trainer.learning_rate, batch_size/(time.time()-btic), name1, loss1, name2, loss2, name3, loss3, name4, loss4))
            btic = time.time()

        name1, loss1 = obj_metrics.get()
        name2, loss2 = center_metrics.get()
        name3, loss3 = scale_metrics.get()
        name4, loss4 = cls_metrics.get()
        logger.info('[Epoch {}] Training cost: {:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}, {}={:.3f}'.format(
            epoch, (time.time()-tic), name1, loss1, name2, loss2, name3, loss3, name4, loss4))
        if not (epoch + 1) % args.val_interval:
            print("validate:", epoch + 1)
            # consider reduce the frequency of validation to save time
            map_name, mean_ap = validate(net, val_data, ctx, eval_metric)
            #map_name_train, mean_ap_train = validate(net, train_data, ctx, eval_metric)
            print('MAP PRINTINNNNNNGGGG')
            if isinstance(map_name, list):
                val_msg = '\n'.join(['{}={}'.format(k, v) for k, v in zip(map_name, mean_ap)])
                #train_msg = '\n'.join(['{}={}'.format(k, v) for k, v in zip(map_name_train, mean_ap_train)])
                current_map = float(mean_ap[-1])
            else:
                val_msg='{}={}'.format(map_name, mean_ap)
                #train_msg='{}={}'.format(map_name_train, mean_ap_train)
                current_map = mean_ap
            logger.info('[Epoch {}] Validation: {} ;'.format(epoch, val_msg))
            #logger.info('[Epoch {}] Train: {} ;'.format(epoch, train_msg))     
        else:
            current_map = 0.
        save_params(net, best_map, current_map, epoch, args.save_interval, os.path.join(args.model_dir, 'yolov3'))
        

if __name__ == '__main__':
    args = parse_args()
    # fix seed for mxnet, numpy and python builtin random generator.
    gutils.random.seed(args.seed)

    # training contexts
    ctx = [mx.gpu(int(i)) for i in range(args.num_gpus)]
    ctx = ctx if ctx else [mx.cpu()]

    # network
    net_name = args.network
    args.save_prefix += net_name
    # use sync bn if specified
    num_sync_bn_devices = len(ctx) if args.syncbn else -1
    #classes = read_classes(args)
    net = None
    if num_sync_bn_devices > 1:
        print("num_sync_bn_devices > 1")
        if args.pretrained:
            print("use pretrained weights of coco")
            net = get_model(net_name, pretrained=True, num_sync_bn_devices=num_sync_bn_devices)
        else:        
            print("use pretrained weights of mxnet")
            net = get_model(net_name, pretrained_base=True, num_sync_bn_devices=num_sync_bn_devices)

        #net.reset_class(classes)            
        async_net = get_model(net_name, pretrained_base=False)  # used by cpu worker
    else:
        print("num_sync_bn_devices <= 1")
        net = get_model(net_name, pretrained=True)
        #if args.pretrained:
        #    net = get_model(net_name, pretrained=True)            
        #else:
        #    net = get_model(net_name, pretrained_base=True)
        #net.reset_class(classes)            
        async_net = net

    if args.resume.strip():
        net.load_parameters(args.resume.strip())
        async_net.load_parameters(args.resume.strip())
    else:
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            net.initialize()
            async_net.initialize()

    # training data
    train_dataset, val_dataset, eval_metric = get_dataset(args)
    train_data, val_data = get_dataloader(
        async_net, train_dataset, val_dataset, args.data_shape, args.batch_size, args.num_workers, args)

    # training
    train(net, train_data, val_data, eval_metric, ctx, args)
