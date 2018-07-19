#!/usr/bin/env python
# -*- coding:utf-8 -*-
# Author: Donny You(youansheng@gmail.com)


from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable

from utils.layers.det.yolo_detection_layer import YOLODetectionLayer
from utils.helpers.det_helper import DetHelper
from utils.tools.logger import Logger as Log


class SSDFocalLoss(nn.Module):
    def __init__(self, configer):
        super(SSDFocalLoss, self).__init__()
        self.num_classes = configer.get('data', 'num_classes')

    def _one_hot_embeding(self, labels):
        """Embeding labels to one-hot form.

        Args:
            labels(LongTensor): class labels
            num_classes(int): number of classes
        Returns:
            encoded labels, sized[N, #classes]

        """

        y = torch.eye(self.num_classes)  # [D, D]
        return y[labels]  # [N, D]

    def focal_loss(self, x, y):
        """Focal loss

        Args:
            x(tensor): size [N, D]
            y(tensor): size [N, ]
        Returns:
            (tensor): focal loss

        """

        alpha = 0.25
        gamma = 2

        t = self._one_hot_embeding(y.data.cpu())
        t = Variable(t).cuda()  # [N, 20]

        logit = F.softmax(x)
        logit = logit.clamp(1e-7, 1.-1e-7)
        conf_loss_tmp = -1 * t.float() * torch.log(logit)
        conf_loss_tmp = alpha * conf_loss_tmp * (1-logit)**gamma
        conf_loss = conf_loss_tmp.sum()

        return conf_loss

    def forward(self, loc_preds, loc_targets, cls_preds, cls_targets):
        """Compute loss between (loc_preds, loc_targets) and (cls_preds, cls_targets).

        Args:
          loc_preds(tensor): predicted locations, sized [batch_size, #anchors, 4].
          loc_targets(tensor): encoded target locations, sized [batch_size, #anchors, 4].
          cls_preds(tensor): predicted class confidences, sized [batch_size, #anchors, #classes].
          cls_targets(tensor): encoded target labels, sized [batch_size, #anchors].
        Returns:
          (tensor) loss = SmoothL1Loss(loc_preds, loc_targets) + FocalLoss(cls_preds, cls_targets).

        """

        pos = cls_targets > 0  # [N,#anchors]
        num_pos = pos.data.long().sum()

        # loc_loss = SmoothL1Loss(pos_loc_preds, pos_loc_targets)
        mask = pos.unsqueeze(2).expand_as(loc_preds)  # [N,#anchors,4]
        masked_loc_preds = loc_preds[mask].view(-1, 4)  # [#pos,4]
        masked_loc_targets = loc_targets[mask].view(-1, 4)  # [#pos,4]
        loc_loss = F.smooth_l1_loss(masked_loc_preds, masked_loc_targets, size_average=False)

        # cls_loss = FocalLoss(loc_preds, loc_targets)
        pos_neg = cls_targets > -1  # exclude ignored anchors
        # num_pos_neg = pos_neg.data.long().sum()
        mask = pos_neg.unsqueeze(2).expand_as(cls_preds)
        masked_cls_preds = cls_preds[mask].view(-1, self.num_classes)
        cls_loss = self.focal_loss(masked_cls_preds, cls_targets[pos_neg])

        num_pos = max(1.0, num_pos)

        Log.debug('loc_loss: %.3f | cls_loss: %.3f' % (loc_loss.data[0] / num_pos, cls_loss.data[0] / num_pos))

        loss = loc_loss / num_pos + cls_loss / num_pos

        return loss


class SSDMultiBoxLoss(nn.Module):

    def __init__(self, configer):
        super(SSDMultiBoxLoss, self).__init__()
        self.num_classes = configer.get('data', 'num_classes')

    def _cross_entropy_loss(self, x, y):
        """Cross entropy loss w/o averaging across all samples.

        Args:
          x(tensor): sized [N,D]
          y(tensor): sized [N,]

        Returns:
          (tensor): cross entropy loss, sized [N,]

        """
        xmax = x.data.max()
        log_sum_exp = torch.log(torch.sum(torch.exp(x - xmax), dim=1)) + xmax
        return log_sum_exp.view(-1, 1) - x.gather(1, y.view(-1, 1))

    def test_cross_entropy_loss(self):
        a = Variable(torch.randn(10, 4))
        b = Variable(torch.ones(10).long())
        loss = self.cross_entropy_loss(a, b)
        print(loss.mean())
        print(F.cross_entropy(a, b))

    def _hard_negative_mining(self, conf_loss, pos):
        """Return negative indices that is 3x the number as positive indices.

        Args:
          conf_loss: (tensor) cross entropy loss between conf_preds and conf_targets, sized [N*8732,]
          pos: (tensor) positive(matched) box indices, sized [N, 8732]
        Returns:
          (tensor): negative indices, sized [N, 8732]

        """
        batch_size, num_boxes = pos.size()

        conf_loss = conf_loss.view(batch_size, -1)  # [N,8732]
        conf_loss[pos] = 0  # set pos boxes = 0, the rest are neg conf_loss

        _, idx = conf_loss.sort(1, descending=True)  # sort by neg conf_loss
        _, rank = idx.sort(1)  # [N,8732]

        num_pos = pos.long().sum(1)  # [N,1]
        num_neg = torch.clamp(3 * num_pos, min=1, max=num_boxes-1)  # [N,1]
        neg = rank < num_neg.unsqueeze(1).expand_as(rank)  # [N,8732]

        return neg

    def forward(self, loc_preds, loc_targets, conf_preds, conf_targets):
        """Compute loss between (loc_preds, loc_targets) and (conf_preds, conf_targets).

        Args:
          loc_preds(tensor): predicted locations, sized [batch_size, 8732, 4]
          loc_targets(tensor): encoded target locations, sized [batch_size, 8732, 4]
          conf_preds(tensor): predicted class confidences, sized [batch_size, 8732, num_classes]
          conf_targets:(tensor): encoded target classes, sized [batch_size, 8732]
          is_print: whether print loss
          img: using for visualization

        loss:
          (tensor) loss = SmoothL1Loss(loc_preds, loc_targets) + CrossEntropyLoss(conf_preds, conf_targets)
          loc_loss = SmoothL1Loss(pos_loc_preds, pos_loc_targets)
          conf_loss = CrossEntropyLoss(pos_conf_preds, pos_conf_targets)
                    + CrossEntropyLoss(neg_conf_preds, neg_conf_targets)

        """
        batch_size, num_boxes, _ = loc_preds.size()

        pos = conf_targets > 0  # [N,8732], pos means the box matched.
        num_matched_boxes = pos.data.float().sum()
        if num_matched_boxes == 0:
            print("No matched boxes")

        # loc_loss.
        pos_mask = pos.unsqueeze(2).expand_as(loc_preds)  # [N, 8732, 4]
        pos_loc_preds = loc_preds[pos_mask].view(-1, 4)  # [pos,4]
        pos_loc_targets = loc_targets[pos_mask].view(-1, 4)  # [pos,4]
        loc_loss = F.smooth_l1_loss(pos_loc_preds, pos_loc_targets, size_average=False)

        # conf_loss.
        conf_loss = self._cross_entropy_loss(conf_preds.view(-1, self.num_classes), conf_targets.view(-1))  # [N*8732,]
        neg = self._hard_negative_mining(conf_loss, pos)    # [N,8732]
        pos_mask = pos.unsqueeze(2).expand_as(conf_preds)  # [N,8732,21]
        neg_mask = neg.unsqueeze(2).expand_as(conf_preds)  # [N,8732,21]
        mask = (pos_mask + neg_mask).gt(0)
        pos_and_neg = (pos + neg).gt(0)
        preds = conf_preds[mask].view(-1, self.num_classes)  # [pos + neg,21]
        targets = conf_targets[pos_and_neg]                  # [pos + neg,]
        conf_loss = F.cross_entropy(preds, targets, size_average=False)

        if num_matched_boxes > 0:
            loc_loss = loc_loss / num_matched_boxes
            conf_loss = conf_loss / num_matched_boxes
        else:
            return conf_loss + loc_loss

        Log.debug("loc_loss: %f, cls_loss: %f" % (float(loc_loss.item()), float(conf_loss.item())))

        return loc_loss + conf_loss


class YOLOv3Loss(nn.Module):
    def __init__(self, configer):
        super(YOLOv3Loss, self).__init__()

        self.configer = configer
        self.yolo_detection_layer = YOLODetectionLayer(self.configer)
        self.num_classes = self.configer.get('data', 'num_classes')
        self.img_size = self.configer.get('data', 'train_input_size')
        self.lambda_xy = self.configer.get('network', 'loss_weights')['coord_loss']  # 2.5
        self.lambda_wh = self.configer.get('network', 'loss_weights')['coord_loss']
        self.lambda_conf = self.configer.get('network', 'loss_weights')['obj_loss']  # 1.0
        self.lambda_cls = self.configer.get('network', 'loss_weights')['cls_loss']  # 1.0

        self.mse_loss = nn.MSELoss()
        self.bce_loss = nn.BCELoss()

    def forward(self, outputs_list, targets, objmask, noobjmask):
        prediction_list = list()
        for i, outputs in enumerate(outputs_list):
            prediction_list.append(self.yolo_detection_layer(outputs,
                                                             self.configer.get('gt', 'anchors')[i],
                                                             is_training=True))
        prediction = torch.cat(prediction_list, 1)

        # Get outputs
        x = prediction[..., 0]  # Center x
        y = prediction[..., 1]  # Center y
        w = prediction[..., 2]  # Width
        h = prediction[..., 3]  # Height
        conf = prediction[..., 4]  # Conf
        pred_cls = prediction[..., 5:]  # Cls pred.

        # Get targets
        tx = targets[..., 0]  # Center x
        ty = targets[..., 1]  # Center y
        tw = targets[..., 2]  # Width
        th = targets[..., 3]  # Height
        tconf = targets[..., 4]  # Conf
        tcls = targets[..., 5:]  # Cls pred.

        mask, noobj_mask = objmask.cuda(), noobjmask.cuda()
        tx, ty, tw, th = tx.cuda(), ty.cuda(), tw.cuda(), th.cuda()
        tconf, tcls = tconf.cuda(), tcls.cuda()
        #  losses.
        loss_x = self.bce_loss(x * mask, tx * mask)
        loss_y = self.bce_loss(y * mask, ty * mask)
        loss_w = self.mse_loss(w * mask, tw * mask)
        loss_h = self.mse_loss(h * mask, th * mask)
        loss_conf = self.bce_loss(conf * mask, mask) + \
                    0.5 * self.bce_loss(conf * noobj_mask, noobj_mask * 0.0)
        loss_cls = self.bce_loss(pred_cls[mask == 1], tcls[mask == 1])
        #  total loss = losses * weight
        loss = loss_x * self.lambda_xy + loss_y * self.lambda_xy + \
               loss_w * self.lambda_wh + loss_h * self.lambda_wh + \
               loss_conf * self.lambda_conf + loss_cls * self.lambda_cls

        return loss


class FRLoss(nn.Module):

    def __init__(self, configer):
        super(FRLoss, self).__init__()
        self.configer = configer

        self.lambda_xy = self.configer.get('network', 'loss_weights')['coord_loss']  # 2.5
        self.lambda_wh = self.configer.get('network', 'loss_weights')['coord_loss']
        self.lambda_conf = self.configer.get('network', 'loss_weights')['obj_loss']  # 1.0
        self.lambda_cls = self.configer.get('network', 'loss_weights')['cls_loss']  # 1.0

    def forward(self, output_list, target_list):
        return None