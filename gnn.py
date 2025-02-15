import torch
import torch.nn.functional as F
import torch.nn as nn
from utils import *
import time
from multiprocessing import Pool
from layers import GGNN, GConv, GConv_gate, GAT_gate, ConcreteDropout

N_atom_features = 28

class gnn(torch.nn.Module):
    def __init__(self, args):
        super(gnn, self).__init__()
        n_graph_layer = args.n_graph_layer
        d_graph_layer = args.d_graph_layer
        n_FC_layer = args.n_FC_layer
        d_FC_layer = args.d_FC_layer
        self.dropout_rate = args.dropout_rate 


        self.layers1 = [d_graph_layer for i in range(n_graph_layer+1)]
        if args.GNN == 'GGNN':
            self.gconv1 = nn.ModuleList([GGNN(self.layers1[i], self.layers1[i+1]) for i in range(len(self.layers1)-1)]) 
        elif args.GNN == 'GConv':
            self.gconv1 = nn.ModuleList([GConv(self.layers1[i], self.layers1[i+1]) for i in range(len(self.layers1)-1)]) 
        elif args.GNN == 'GConv_gate':
            self.gconv1 = nn.ModuleList([GConv_gate(self.layers1[i], self.layers1[i+1]) for i in range(len(self.layers1)-1)]) 
        elif args.GNN == 'GAT_gate':
            self.gconv1 = nn.ModuleList([GAT_gate(self.layers1[i], self.layers1[i+1]) for i in range(len(self.layers1)-1)]) 
        
        self.FC = nn.ModuleList([nn.Linear(self.layers1[-1], d_FC_layer) if i==0 else
                                 nn.Linear(d_FC_layer, 1) if i==n_FC_layer-1 else
                                 nn.Linear(d_FC_layer, d_FC_layer) for i in range(n_FC_layer)])
        
        self.mu = nn.Parameter(torch.Tensor([args.initial_mu]).float())
        self.dev = nn.Parameter(torch.Tensor([args.initial_dev]).float())
        self.embede = nn.Linear(2*N_atom_features, d_graph_layer, bias = False)
        
        #Variables for concrete dropout

        self.CDO = False
        if args.CDO==True:
            self.CDO = args.CDO
            self.l = args.CDO_l
            self.N = args.CDO_N
            weight_regularizer = self.l**2. / self.N
            dropout_regularizer = 2.0 / self.N
            self.CDO1 = nn.ModuleList([ConcreteDropout(weight_regularizer=weight_regularizer,\
                                                     dropout_regularizer=dropout_regularizer) \
                                                     for i in range(len(self.layers1)-1)])
            self.CDO2 = nn.ModuleList([ConcreteDropout(weight_regularizer=weight_regularizer,\
                                                     dropout_regularizer=dropout_regularizer) \
                                                     for i in range(len(self.FC)-1)])


    def embede_graph(self, data):
        c_hs, c_adjs1, c_adjs2, c_valid = data
        c_hs = self.embede(c_hs)
        hs_size = c_hs.size()
        c_adjs2 = torch.exp(-torch.pow(c_adjs2-self.mu.expand_as(c_adjs2), 2)/self.dev) + c_adjs1
        regularization = torch.empty(len(self.gconv1), device=c_hs.device)

        for k in range(len(self.gconv1)):
            if self.CDO:
                c_hs, regularization[k] = self.CDO1[k](c_hs, self.gconv1[k], c_adjs1, c_adjs2)
            else:
                c_hs1 = self.gconv1[k](c_hs, c_adjs1)
                c_hs2 = self.gconv1[k](c_hs, c_adjs2)
                c_hs = c_hs2-c_hs1
                c_hs = F.dropout(c_hs, p=self.dropout_rate, training=self.training)
        c_hs = c_hs*c_valid.unsqueeze(-1).repeat(1, 1, c_hs.size(-1))
        c_hs = c_hs.sum(1)
        if self.CDO:
            return c_hs, regularization.sum()
        else:            
            return c_hs, 0.0

    def fully_connected(self, c_hs):
        regularization = torch.empty(len(self.FC)*1-1, device=c_hs.device)

        for k in range(len(self.FC)):
            #c_hs = self.FC[k](c_hs)
            if k<len(self.FC)-1:
                if self.CDO:
                    c_hs, regularization[k] = self.CDO2[k](c_hs, self.FC[k])
                else:
                    c_hs = self.FC[k](c_hs)
                    c_hs = F.dropout(c_hs, p=self.dropout_rate, training=self.training)
                c_hs = F.relu(c_hs)
            else:
                c_hs = self.FC[k](c_hs)

        c_hs = torch.sigmoid(c_hs)

        #return retval, 0.0
        if self.CDO:
            return c_hs, regularization.sum()
        else:
            return c_hs, 0.0

    def train_model(self, data1, data2, mixing_ratio):
        #embede a graph to a vector
        c_hs1, regularization1 = self.embede_graph(data1)
        c_hs2, regularization2 = self.embede_graph(data2)

        if mixing_ratio is not None:
            #linearly interpolate between graph of active protein-ligand complex and inactive protein-ligand complex
            ratio = mixing_ratio.unsqueeze(-1).repeat(1, c_hs1.size(-1))
            c_hs = c_hs1*ratio + c_hs2*(1-ratio)
        else:
            c_hs = torch.cat([c_hs1,c_hs2],0)
        
        #fully connected NN
        c_hs, regularization3 = self.fully_connected(c_hs)
        c_hs = c_hs.view(-1) 

        #note that if you don't use concrete dropout, regularization 1-3 is zero
        return c_hs, regularization1+regularization2+regularization3
    
    def test_model(self,data1 ):
        c_hs, _ = self.embede_graph(data1)
        c_hs, _ = self.fully_connected(c_hs)
        c_hs = c_hs.view(-1)
        return c_hs
