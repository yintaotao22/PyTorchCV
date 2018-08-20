#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Donny You(youansheng@gmail.com)
# The class of DenseASPPDetecNet


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import torch
from torch.nn import functional as F
from torch import nn
from torchvision.models import vgg16

from utils.layers.det.fr_roi_generator import FRRoiGenerator
from utils.layers.det.roi_pooling_layer import ROIPoolingLayer, PyROIPoolingLayer
from utils.layers.det.roi_sample_layer import RoiSampleLayer
from utils.tools.logger import Logger as Log


DETECTOR_CONFIG = {
    'vgg_cfg': [64, 64, 'M', 128, 128, 'M', 256, 256, 256, 'M', 512, 512, 512, 'M', 512, 512, 512]
}


class VGGModel(object):
    def __init__(self, configer):
        self.configer = configer

    def __call__(self):
        # the 30th layer of features is relu of conv5_3
        model = vgg16(pretrained=False)
        if self.configer.get('network', 'pretrained'):
            if self.configer.is_empty('network', 'caffe_pretrained'):
                Log.info('Loading pretrained model: {}'.format(self.configer.get('network', 'pytorch_pretrained')))
                model.load_state_dict(torch.load(self.configer.get('network', 'pytorch_pretrained')))

            else:
                Log.info('Loading pretrained model: {}'.format(self.configer.get('network', 'caffe_pretrained')))
                model.load_state_dict(torch.load(self.configer.get('network', 'caffe_pretrained')))

        features = list(model.features)[:30]
        classifier = model.classifier

        classifier = list(classifier)
        del classifier[6]
        if not self.configer.get('network', 'use_drop'):
            del classifier[5]
            del classifier[2]

        classifier = nn.Sequential(*classifier)

        # freeze top4 conv
        for layer in features[:10]:
            for p in layer.parameters():
                p.requires_grad = False

        return nn.Sequential(*features), classifier


class FasterRCNN(nn.Module):

    def __init__(self, configer):
        super(FasterRCNN, self).__init__()
        self.configer = configer
        self.extractor, self.classifier = VGGModel(configer)()
        self.rpn = NaiveRPN(configer)
        self.roi = FRRoiGenerator(configer)
        self.roi_sampler = RoiSampleLayer(configer)
        self.roi_head = RoIHead(configer, self.classifier)

    def forward(self, *inputs):
        """Forward Faster R-CNN.
        Scaling paramter :obj:`scale` is used by RPN to determine the
        threshold to select small objects, which are going to be
        rejected irrespective of their confidence scores.
        Here are notations used.
        * :math:`N` is the number of batch size
        * :math:`R'` is the total number of RoIs produced across batches. \
            Given :math:`R_i` proposed RoIs from the :math:`i` th image, \
            :math:`R' = \\sum _{i=1} ^ N R_i`.
        * :math:`L` is the number of classes excluding the background.
        Classes are ordered by the background, the first class, ..., and
        the :math:`L` th class.
        Args:
            x (autograd.Variable): 4D image variable.
            scale (float): Amount of scaling applied to the raw image
                during preprocessing.
        Returns:
            Variable, Variable, array, array:
            Returns tuple of four values listed below.
            * **roi_cls_locs**: Offsets and scalings for the proposed RoIs. \
                Its shape is :math:`(R', (L + 1) \\times 4)`.
            * **roi_scores**: Class predictions for the proposed RoIs. \
                Its shape is :math:`(R', L + 1)`.
            * **rois**: RoIs proposed by RPN. Its shape is \
                :math:`(R', 4)`.
            * **roi_indices**: Batch indices of RoIs. Its shape is \
                :math:`(R',)`.
        """
        if self.configer.get('phase') == 'test' and not self.training:
            x = self.extractor(inputs[0])
            rpn_locs, rpn_scores = self.rpn(x)

            indices_and_rois = self.roi(rpn_locs, rpn_scores,
                                        self.configer.get('rpn', 'n_test_pre_nms'),
                                        self.configer.get('rpn', 'n_test_post_nms'))
            roi_cls_locs, roi_scores = self.roi_head(x, indices_and_rois)
            return indices_and_rois, roi_cls_locs, roi_scores

        elif self.configer.get('phase') == 'train' and not self.training:
            x, gt_bboxes, gt_bboxes_num, gt_labels = inputs
            x = self.extractor(x)
            rpn_locs, rpn_scores = self.rpn(x)
            test_indices_and_rois = self.roi(rpn_locs, rpn_scores,
                                             self.configer.get('rpn', 'n_test_pre_nms'),
                                             self.configer.get('rpn', 'n_test_post_nms'))
            test_roi_cls_locs, test_roi_scores = self.roi_head(x, test_indices_and_rois)

            test_group = [test_indices_and_rois, test_roi_cls_locs, test_roi_scores]
            train_indices_and_rois = self.roi(rpn_locs, rpn_scores,
                                             self.configer.get('rpn', 'n_train_pre_nms'),
                                             self.configer.get('rpn', 'n_train_post_nms'))

            sample_rois, gt_roi_bboxes, gt_roi_labels = self.roi_sampler(train_indices_and_rois,
                                                                         gt_bboxes, gt_bboxes_num, gt_labels)

            sample_roi_locs, sample_roi_scores = self.roi_head(x, sample_rois)
            sample_roi_locs = sample_roi_locs.contiguous().view(-1, self.configer.get('data', 'num_classes'), 4)
            sample_roi_locs = sample_roi_locs[
                torch.arange(0, sample_roi_locs.size()[0]).long().to(sample_roi_locs.device),
                gt_roi_labels.long().to(sample_roi_locs.device)].contiguous().view(-1, 4)

            train_group = [rpn_locs, rpn_scores, sample_roi_locs, sample_roi_scores, gt_roi_bboxes, gt_roi_labels]
            return train_group, test_group

        elif self.configer.get('phase') == 'train' and self.training:
            x, gt_bboxes, gt_bboxes_num, gt_labels = inputs
            x = self.extractor(x)
            rpn_locs, rpn_scores = self.rpn(x)

            train_indices_and_rois = self.roi(rpn_locs, rpn_scores,
                                              self.configer.get('rpn', 'n_train_pre_nms'),
                                              self.configer.get('rpn', 'n_train_post_nms'))

            sample_rois, gt_roi_bboxes, gt_roi_labels = self.roi_sampler(train_indices_and_rois,
                                                                         gt_bboxes, gt_bboxes_num, gt_labels)

            sample_roi_locs, sample_roi_scores = self.roi_head(x, sample_rois)
            sample_roi_locs = sample_roi_locs.contiguous().view(-1, self.configer.get('data', 'num_classes'), 4)
            sample_roi_locs = sample_roi_locs[
                torch.arange(0, sample_roi_locs.size()[0]).long().to(sample_roi_locs.device),
                gt_roi_labels.long().to(sample_roi_locs.device)].contiguous().view(-1, 4)

            return [rpn_locs, rpn_scores, sample_roi_locs, sample_roi_scores, gt_roi_bboxes, gt_roi_labels]

        else:
            Log.error('Invalid Status.')
            exit(1)


class NaiveRPN(nn.Module):
    def __init__(self, configer):
        super(NaiveRPN, self).__init__()
        self.configer = configer
        self.num_anchor_list = self.configer.get('rpn', 'num_anchor_list')
        self.conv1 = nn.Conv2d(512, 512, 3, 1, 1)
        self.score = nn.Conv2d(512, self.num_anchor_list[0] * 2, 1, 1, 0)
        self.loc = nn.Conv2d(512, self.num_anchor_list[0] * 4, 1, 1, 0)

    def forward(self, x):
        h = F.relu(self.conv1(x))

        rpn_locs = self.loc(h)
        rpn_locs = rpn_locs.permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, 4)
        rpn_scores = self.score(h)
        rpn_scores = rpn_scores.permute(0, 2, 3, 1).contiguous().view(x.size(0), -1, 2)
        return rpn_locs, rpn_scores


class RoIHead(nn.Module):
    """Faster R-CNN Head for VGG-16 based implementation.
    This class is used as a head for Faster R-CNN.
    This outputs class-wise localizations and classification based on feature
    maps in the given RoIs.

    Args:
        n_class (int): The number of classes possibly including the background.
        roi_size (int): Height and width of the feature maps after RoI-pooling.
        spatial_scale (float): Scale of the roi is resized.
        classifier (nn.Module): Two layer Linear ported from vgg16
    """

    def __init__(self, configer, classifier):
        # n_class includes the background
        super(RoIHead, self).__init__()
        self.configer = configer
        self.classifier = classifier
        self.cls_loc = nn.Linear(4096, self.configer.get('data', 'num_classes') * 4)
        self.score = nn.Linear(4096, self.configer.get('data', 'num_classes'))
        self.roi_layer = ROIPoolingLayer(self.configer)

    def forward(self, x, indices_and_rois):
        """Forward the chain.
        We assume that there are :math:`N` batches.
        Args:
            x (Variable): 4D image variable.
            rois (Tensor): A bounding box array containing coordinates of
                proposal boxes.  This is a concatenation of bounding box
                arrays from multiple images in the batch.
                Its shape is :math:`(R', 4)`. Given :math:`R_i` proposed
                RoIs from the :math:`i` th image,
                :math:`R' = \\sum _{i=1} ^ N R_i`.
            roi_indices (Tensor): An array containing indices of images to
                which bounding boxes correspond to. Its shape is :math:`(R',)`.
        """
        # in case roi_indices is  ndarray

        pool = self.roi_layer(x, indices_and_rois)
        pool = pool.view(pool.size(0), -1)
        fc7 = self.classifier(pool)
        roi_cls_locs = self.cls_loc(fc7)
        roi_scores = self.score(fc7)
        return roi_cls_locs, roi_scores
