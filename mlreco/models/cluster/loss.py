import torch
import torch.nn as nn
import numpy as np
import sparseconvnet as scn
from collections import defaultdict


class DiscriminativeLoss(torch.nn.Module):
    '''
    Implementation of the Discriminative Loss Function in Pytorch.
    https://arxiv.org/pdf/1708.02551.pdf
    Note that there are many other implementations in Github, yet here
    we tailor it for use in conjuction with Sparse UResNet.
    '''

    def __init__(self, cfg, reduction='sum'):
        super(DiscriminativeLoss, self).__init__()
        self._cfg = cfg['modules']['clustering_loss']
        self._num_classes = self._cfg.get('num_classes', 5)
        self._depth = self._cfg.get('stride', 5)

        # Clustering Loss Parameters
        self.loss_hyperparams = {}
        self.loss_hyperparams['intra_weight'] = self._cfg.get('intra_weight', 1.0)
        self.loss_hyperparams['inter_weight'] = self._cfg.get('inter_weight', 1.0)
        self.loss_hyperparams['reg_weight'] = self._cfg.get('reg_weight', 0.001)
        self.loss_hyperparams['intra_margin'] = self._cfg.get('intracluster_margin', 0.5)
        self.loss_hyperparams['inter_margin'] = self._cfg.get('intercluster_margin', 1.5)

        self._dimension = self._cfg.get('data_dim', 3)
        self._norm = self._cfg.get('norm', 2)
        self._seg_contingent = self._cfg.get('contingent', True)

    def find_cluster_means(self, features, labels):
        '''
        For a given image, compute the centroids mu_c for each
        cluster label in the embedding space.
        Inputs:
            features (torch.Tensor): the pixel embeddings, shape=(N, d) where
            N is the number of pixels and d is the embedding space dimension.
            labels (torch.Tensor): ground-truth group labels, shape=(N, )
        Returns:
            cluster_means (torch.Tensor): (n_c, d) tensor where n_c is the number of
            distinct instances. Each row is a (1,d) vector corresponding to
            the coordinates of the i-th centroid.
        '''
        clabels = labels.unique(sorted=True)
        cluster_means = []
        for c in clabels:
            index = (labels == c)
            mu_c = features[index].mean(0)
            cluster_means.append(mu_c)
        cluster_means = torch.stack(cluster_means)
        return cluster_means

    def intra_cluster_loss(self, features, labels, cluster_means, margin=0.5):
        '''
        Implementation of variance loss in Discriminative Loss.
        Inputs:
            features (torch.Tensor): pixel embedding, same as in find_cluster_means.
            labels (torch.Tensor): ground truth instance labels
            cluster_means (torch.Tensor): output from find_cluster_means
            margin (float/int): constant used to specify delta_v in paper. Think of it
            as the size of each clusters in embedding space. 
        Returns:
            var_loss: (float) variance loss (see paper).
        '''
        var_loss = 0.0
        n_clusters = len(cluster_means)
        cluster_labels = labels.unique(sorted=True)
        for i, c in enumerate(cluster_labels):
            index = (labels == c)
            dists = torch.norm(features[index] - cluster_means[i] + 1e-8,
                               p=self._norm,
                               dim=1)
            hinge = torch.clamp(dists - margin, min=0)
            l = torch.mean(torch.pow(hinge, 2))
            var_loss += l
        var_loss /= n_clusters
        return var_loss

    def inter_cluster_loss(self, cluster_means, margin=1.5):
        '''
        Implementation of distance loss in Discriminative Loss.
        Inputs:
            cluster_means (torch.Tensor): output from find_cluster_means
            margin (float/int): the magnitude of the margin delta_d in the paper.
            Think of it as the distance between each separate clusters in
            embedding space.
        Returns:
            dist_loss (float): computed cross-centroid distance loss (see paper).
            Factor of 2 is included for proper normalization.
        '''
        dist_loss = 0.0
        n_clusters = len(cluster_means)
        if n_clusters < 2:
            # Inter-cluster loss is zero if there only one instance exists for
            # a semantic label.
            return 0.0
        else:
            for i, c1 in enumerate(cluster_means):
                for j, c2 in enumerate(cluster_means):
                    if i != j:
                        dist = torch.norm(c1 - c2 + 1e-8, p=self._norm)
                        hinge = torch.clamp(2.0 * margin - dist, min=0)
                        dist_loss += torch.pow(hinge, 2)
            dist_loss /= float((n_clusters - 1) * n_clusters)
            return dist_loss

    def regularization(self, cluster_means):
        '''
        Implementation of regularization loss in Discriminative Loss
        Inputs:
            cluster_means (torch.Tensor): output from find_cluster_means
        Returns:
            reg_loss (float): computed regularization loss (see paper).
        '''
        reg_loss = 0.0
        n_clusters, _ = cluster_means.shape
        for i in range(n_clusters):
            reg_loss += torch.norm(cluster_means[i, :] + 1e-8, p=self._norm)
        reg_loss /= float(n_clusters)
        return reg_loss

    def compute_heuristic_accuracy(self, embedding, truth, bandwidth=0.5):
        '''
        Compute Adjusted Rand Index Score for given embedding coordinates,
        where predicted cluster labels are obtained from distance to closest
        centroid (computes heuristic accuracy). 

        Inputs:
            embedding (torch.Tensor): (N, d) Tensor where 'd' is the embedding dimension.
            truth (torch.Tensor): (N, ) Tensor for the ground truth clustering labels.
        Returns:
            score (float): Computed ARI Score
            clustering (array): the predicted cluster labels.
        '''
        from sklearn.metrics import adjusted_rand_score
        nearest = []
        with torch.no_grad():
            cmeans = self.find_cluster_means(embedding, truth)
            for centroid in cmeans:
                dists = torch.sum((embedding - centroid)**2, dim=1)
                dists = dists.view(-1, 1)
                nearest.append(dists)
            nearest = torch.cat(nearest, dim=1)
            nearest = torch.argmin(nearest, dim=1)
            pred = nearest.cpu().numpy()
            grd = truth.cpu().numpy()
            score = adjusted_rand_score(pred, grd)
        return score

    def combine(self, features, labels, **kwargs):
        '''
        Wrapper function for combining different components of the loss function.
        Inputs:
            features (torch.Tensor): pixel embeddings
            labels (torch.Tensor): ground-truth instance labels
        Returns:
            loss: combined loss, in most cases over a given semantic class.
        '''
        # Clustering Loss Hyperparameters
        # We allow changing the parameters at each computation in order
        # to alter the margins at each spatial resolution in multi-scale losses. 
        delta_var = kwargs.get('intra_margin', 0.5)
        delta_dist = kwargs.get('inter_margin', 1.5)
        intra_weight = kwargs.get('intra_weight', 1.0)
        inter_weight = kwargs.get('inter_weight', 1.0)
        reg_weight = kwargs.get('reg_weight', 1.0)

        c_means = self.find_cluster_means(features, labels)
        inter_loss = self.inter_cluster_loss(c_means, margin=delta_var)
        intra_loss = self.intra_cluster_loss(features,
                                           labels,
                                           c_means,
                                           margin=delta_dist)
        reg_loss = self.regularization(c_means)

        loss = intra_weight * loss_var + inter_weight * loss_dist + reg_weight * loss_reg

        return {
            'total_loss': loss, 
            'var_loss': float(intra_loss),
            'dist_loss': float(inter_loss),
            'reg_loss': float(reg_loss)
        }


    def combine_multiclass(self, features, slabels, clabels, **kwargs):
        '''
        Wrapper function for combining different components of the loss, 
        in particular when clustering must be done PER SEMANTIC CLASS. 

        NOTE: When there are multiple semantic classes, we compute the DLoss
        by first masking out by each semantic segmentation (ground-truth/prediction)
        and then compute the clustering loss over each masked point cloud. 

        INPUTS: 
            features (torch.Tensor): pixel embeddings
            slabels (torch.Tensor): semantic labels
            clabels (torch.Tensor): group/instance/cluster labels

        OUTPUT:
            loss_segs (list): list of computed loss values for each semantic class. 
            loss[i] = computed DLoss for semantic class <i>. 
            acc_segs (list): list of computed clustering accuracy for each semantic class. 
        '''
        loss, acc_segs = defaultdict(list), defaultdict(float)
        semantic_classes = slabels.unique()
        for sc in semantic_classes:
            index = (slabels == sc)
            num_clusters = len(clabels[index].unique())
            loss_blob = self.combine(features[index], clabels[index], **kwargs)
            loss['total_loss'].append(loss_blob['total_loss'])
            loss['var_loss'].append(loss_blob['var_loss'])
            loss['dist_loss'].append(loss_blob['dist_loss'])
            loss['reg_loss'].append(loss_blob['reg_loss'])
            acc = self.compute_heuristic_accuracy(features[index], clabels[index])
            acc_segs[sc.item()] = acc
        return loss, acc_segs

    def forward(self, out, semantic_labels, group_labels):
        '''
        Forward function for the Discriminative Loss Module.

        Inputs:
            out: output of UResNet; embedding-space coordinates.
            semantic_labels: ground-truth semantic labels
            group_labels: ground-truth instance labels
        Returns:
            (dict): A dictionary containing key-value pairs for
            loss, accuracy, etc. 
        '''
        num_gpus = len(semantic_labels)
        loss = defaultdict(list)
        accuracy = defaultdict(list)

        for i in range(num_gpus):
            slabels = semantic_labels[i][:, 4]
            slabels = slabels.type(torch.LongTensor)
            clabels = group_labels[i][:, 4]
            batch_idx = semantic_labels[i][:, 3]
            embedding = out['cluster_feature'][i]
            nbatch = int(batch_idx.unique().shape[0])

            for bidx in batch_idx.unique(sorted=True):
                embedding_batch = embedding[batch_idx == bidx]
                slabels_batch = slabels[batch_idx == bidx]
                clabels_batch = clabels[batch_idx == bidx]

                if not self._seg_contingent:
                    loss_dict, acc_segs = self.combine_multiclass(
                        embedding_batch, slabels_batch, clabels_batch, **self.loss_hyperparams)
                    loss["total_loss"].append(
                        sum(loss_dict["total_loss"]) / float(len(loss_dict["total_loss"])))
                    loss["var_loss"].append(
                        sum(loss_dict["var_loss"]) / float(len(loss_dict["var_loss"])))
                    loss["dist_loss"].append(
                        sum(loss_dict["dist_loss"]) / float(len(loss_dict["dist_loss"])))
                    loss["reg_loss"].append(
                        sum(loss_dict["reg_loss"]) / float(len(loss_dict["reg_loss"])))
                    for s, acc in acc_segs.items():
                        accuracy[s].append(acc)
                else:
                    loss["total_loss"].append(self.combine(embedding_batch, clabels_batch, **self.loss_hyperparams))
                    acc, _ = self.compute_heuristic_accuracy(embedding_batch, clabels_batch)
                    accuracy.append(acc)

        total_loss = sum(loss["total_loss"]) / (nbatch * num_gpus)
        var_loss = sum(loss["var_loss"]) / (nbatch * num_gpus)
        dist_loss = sum(loss["dist_loss"]) / (nbatch * num_gpus)
        reg_loss = sum(loss["reg_loss"]) / (nbatch * num_gpus)
        acc_segs = defaultdict(float)
        acc_avg = []
        for i in range(self.num_classes):
            if accuracy[i]:
                acc_segs[i] = sum(accuracy[i]) / float(len(accuracy[i]))
                acc_avg.append(acc_segs[i])
            else:
                acc_segs[i] = 0.0
        acc_avg = sum(acc_avg) / float(len(acc_avg))


        return {
            "loss": total_loss,
            "var_loss": var_loss,
            "dist_loss": dist_loss,
            "reg_loss": reg_loss,
            "accuracy": acc_avg,
            "acc_0": acc_segs[0],
            "acc_1": acc_segs[1],
            "acc_2": acc_segs[2],
            "acc_3": acc_segs[3],
            "acc_4": acc_segs[4]
        }


class MultiScaleLoss(DiscriminativeLoss):

    def __init__(self, cfg):
        super(MultiScaleLoss, self).__init__()
        self._cfg = cfg['modules']['clustering_loss']
        self._num_strides = self._cfg.get('num_strides', 5)

        self._intra_margins = self._cfg.get('intra_margins', 
            [self.loss_hyperparams['intra_margin'] / 2**i for i in range(self._num_strides)])
        self._inter_margins = self._cfg.get('inter_margins',
            [self.loss_hyperparams['inter_margin'] / 2**i for i in range(self._num_strides)])
        

    def compute_loss_layer(self, embedding_scn, slabels, clabels, batch_idx, **kwargs):
        '''
        Compute the multi-class loss for a feature map on a given layer.
        We group the loss computation to a function in order to compute the
        clustering loss over the decoding feature maps.

        INPUTS:
            - embedding (torch.Tensor): (N, d) Tensor with embedding space
                coordinates.
            - slabels (torch.Tensor): (N, 5) Tensor with segmentation labels
            - clabels (torch.Tensor): (N, 5) Tensor with cluster labels
            - batch_idx (list): list of batch indices, ex. [0, 1, ..., 4]

        OUTPUT:
            - loss (torch.Tensor): scalar number (1x1 Tensor) corresponding
                to calculated loss over a given layer.
        '''
        loss = defaultdict(list)
        acc = defaultdict(list)
        coords = embedding_scn.get_spatial_locations().numpy()
        perm = np.lexsort((coords[:, 2], coords[:, 1], coords[:, 0], coords[:, 3]))
        embedding = embedding_scn.features[perm]
        coords = coords[perm]

        for bidx in batch_idx:
            index = slabels[:, 3].int() == bidx
            embedding_batch = embedding[index]
            slabels_batch = slabels[index][:, -1]
            clabels_batch = clabels[index][:, -1]
            # Compute discriminative loss for current event in batch
            if not self._seg_contingent:
                loss_dict, acc_segs = self.combine_multiclass(
                    embedding_batch, slabels_batch, clabels_batch, **kwargs)
                loss["total_loss"].append(
                    sum(loss_dict["total_loss"]) / float(len(loss_dict["total_loss"])))
                loss["var_loss"].append(
                    sum(loss_dict["var_loss"]) / float(len(loss_dict["var_loss"])))
                loss["dist_loss"].append(
                    sum(loss_dict["dist_loss"]) / float(len(loss_dict["dist_loss"])))
                loss["reg_loss"].append(
                    sum(loss_dict["reg_loss"]) / float(len(loss_dict["reg_loss"])))
                for s, acc in acc_segs.items():
                    accuracy[s].append(acc)
            else:
                loss["total_loss"].append(self.combine(embedding_batch, clabels_batch, **kwargs))
                acc, _ = self.compute_heuristic_accuracy(embedding_batch, clabels_batch)
                accuracy.append(acc)

        summed_loss = { key : sum(l) for key, l in loss.items() }
        averaged_acc = { key : sum(l) / float(len(l)) for key, l in acc.items() }
        return summed_loss, averaged_acc


    def forward(self, out, semantic_labels, group_labels):
        '''
        Forward function for the Discriminative Loss Module.

        Inputs:
            out: output of UResNet; embedding-space coordinates.
            semantic_labels: ground-truth semantic labels
            group_labels: ground-truth instance labels
        Returns:
            (dict): A dictionary containing key-value pairs for
            loss, accuracy, etc.
        '''

        loss = defaultdict(list)
        accuracy = defaultdict(float)
        for i_gpu in range(len(semantic_labels)):
            batch_idx = semantic_labels[i][0][:, 3].detach().cpu().int().numpy()
            batch_idx = np.unique(batch_idx)
            batch_size = len(batch_idx)
            # Compute segmentation loss at final layer. 
            loss_seg, acc_seg = self.compute_segmentation_loss(out['segmentation'][i_gpu], semantic_labels[i_gpu][0], batch_idx)
            loss['total_loss'].append(loss_seg)

            # Summing clustering loss over layers.
            for i, em in enumerate(out['cluster_feature'][i]):
                delta_var, delta_dist = self._intra_margins[i], self._inter_margins[i]
                loss_i, acc_i = self.compute_loss_layer(em, semantic_labels[i_gpu][i], group_labels[i_gpu][i], batch_idx,
                                                        delta_var=delta_var, delta_dist=delta_dist)
                for key, val in loss_i.items():
                    loss[key].append(val)
                # Compute accuracy at last layer.
                if i == 0:
                    acc_clustering = acc_i
            for key, acc in acc_clustering.items():
                accuracy[key] = float(acc) * len(batch_idx)

        total_loss = sum(loss["total_loss"])
        var_loss = sum(loss["var_loss"])
        dist_loss = sum(loss["dist_loss"])
        reg_loss = sum(loss["reg_loss"])

        total_acc = 0
        for acc in accuracy.values():
            total_acc += acc / len(accuracy.keys())

        accuracy['acc_seg'] = float(acc_seg)
        accuracy['accuracy'] = total_acc

        res = {
            "loss": total_loss / batch_size,
            "var_loss": var_loss / batch_size,
            "reg_loss": reg_loss / batch_size,
            "dist_loss": dist_loss / batch_size,
            "seg_loss": float(loss_seg),
            "acc_0": accuracy[0],
            "acc_1": accuracy[1],
            "acc_2": accuracy[2],
            "acc_3": accuracy[3],
            "acc_4": accuracy[4],
            "acc_seg": accuracy['acc_seg'],
            "accuracy": accuracy['accuracy'] / batch_size
        }

        return res


class AllyEnemyLoss(MultiScaleLoss):

    def __init__(self, cfg):
        super(AllyEnemyLoss, self).__init__()
        self._cfg = cfg['modules']['clustering_loss']

    def distance_matrix(self):
        pass

    def 


# Sequential Mask Loss
class SequentialMaskLoss(nn.Module):
    pass

# Lovasz Hinge Loss
class LovaszHinge(nn.Module):
    pass

# Lovasz Softmax Loss
class LovaszSoftmax(nn.Module):
    pass

# Regression Loss
class 