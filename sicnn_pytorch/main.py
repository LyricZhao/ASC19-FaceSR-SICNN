from __future__ import print_function
import argparse, os, cv2

import torch
import torch.nn as nn
import torch.optim as optim
import time
import net_sphere

from torch.utils.data import DataLoader
from model import CNNHNet
from dataset import TestDatasetFromFolder, RecDatasetFromFolder
from score import evaluate

def get_test_set(dir):
    return TestDatasetFromFolder(dir + '/valid_HR', dir + '/valid_LR')

def get_train_set(dataset_dir, mapping):
    return RecDatasetFromFolder(dataset_dir + '/HR', dataset_dir + '/LR', mapping)

# Training settings
parser = argparse.ArgumentParser(description='PyTorch Super Res Example')
parser.add_argument('--upscale_factor', type=int, default=4, help="super resolution upscale factor")
parser.add_argument('--bs', type=int, default=256, help='training batch size')
parser.add_argument('--test_bs', type=int, default=256, help='testing batch size')
parser.add_argument('--epochs', type=int, default=200, help='number of epochs to train for')
parser.add_argument('--lr_cnnh', type=float, default=0.001, help='Learning Rate. Default=0.001')
parser.add_argument('--lr_cnnr', type=float, default=0.1, help='Learning Rate. Default=0.1')
parser.add_argument('--threads', type=int, default=8, help='number of threads for data loader to use')
parser.add_argument('--seed', type=int, default=123, help='random seed to use. Default=123')
parser.add_argument('--alpha', type=float, default=10000, help='alpha to combine LSR and LSI in the paper algorithm 1')
parser.add_argument('--test', type=str, default='/home/zhaocg/celeba/dataset', help='path to training dataset')
parser.add_argument('--train', type=str, default='/home/heheda/casia', help='path to cnnr dataset')
parser.add_argument('--label', type=str, default='/home/heheda/casia/mapping.txt', help='path to training dataset')
parser.add_argument('--result', type=str, default='results', help='result dir')
parser.add_argument('--model_output', type=str, default='models', help='model output dir')
options = parser.parse_args()

print(options)

if not torch.cuda.is_available():
    raise Exception('No GPU found, please run without --cuda')

torch.manual_seed(options.seed)
device = torch.device('cuda')

print('[!] Loading datasets ... ', end='', flush=True)
test_set = get_test_set(options.test)
train_set = get_train_set(options.train, options.label)

train_data_loader = DataLoader(dataset=train_set, num_workers=options.threads, batch_size=options.bs, shuffle=False, drop_last=True)
test_data_loader = DataLoader(dataset=test_set, num_workers=options.threads, batch_size=options.test_bs, shuffle=False, drop_last=False)
print('done !', flush=True)

print('[!] Building model ... ', end='', flush=True)
cnn_h = CNNHNet(upscale_factor=options.upscale_factor, batch_size=options.bs)
cnn_h = cnn_h.cuda()

cnn_r = getattr(net_sphere, 'sphere20a')()
cnn_r.load_state_dict(torch.load('sphere20a.pth'))
cnn_r.feature = True

for param in cnn_r.parameters():
    param.requires_grad = False
cnn_r = cnn_r.cuda()

cnn_r_train = getattr(net_sphere, 'sphere20a')()
cnn_r_train = cnn_r_train.cuda()

print('done !', flush=True)

optimizer_cnn_h = optim.SGD(cnn_h.parameters(), lr=options.lr_cnnh, momentum=0.9, weight_decay=0.00025)
optimizer_cnn_r_train = optim.SGD(cnn_r_train.parameters(), lr=options.lr_cnnr, momentum=0.9, weight_decay=5e-4)
EuclideanLoss = nn.MSELoss()
AngleLoss = net_sphere.AngleLoss()

def train(epoch):
    print('[!] Training epoch ' + str(epoch) + ' ...')
    print(' -  Current learning rate is ' + str(options.lr_cnnh) + ' / ' + str(options.lr_cnnr), flush=True)
    bs = options.bs

    for iteration, batch in enumerate(train_data_loader):
        lr, hr, labels = batch[0].cuda(), batch[1].cuda(), batch[2].cuda()
        # --- cnn_r ---
        for param in cnn_h.parameters():
            param.requires_grad = False
        for param in cnn_r_train.parameters():
            param.requires_grad = True
        cnn_r_train.feature = False
        optimizer_cnn_h.zero_grad()
        optimizer_cnn_r_train.zero_grad()

        output_labels = cnn_r_train(torch.cat((sr, hr), 0))
        loss = AngleLoss(output_labels, torch.cat((labels, labels), 0))
        loss.backward()
        optimizer_cnn_r_train.step()
        
        # --- cnn_h --- 
        for param in cnn_h.parameters():
            param.requires_grad = True
        for param in cnn_r_train.parameters():
            param.requires_grad = False
        cnn_r_train.feature = True
        optimizer_cnn_h.zero_grad()
        optimizer_cnn_r_train.zero_grad()

        sr = cnn_h(lr)
        l_sr = EuclideanLoss(sr, hr)
        features = cnn_r_train(torch.cat((sr, hr), 0))
        f1 = features[0:bs, :]; f2 = features[bs:, :]
        l_si = EuclideanLoss(f1, f2.detach())
        loss = l_sr + options.alpha * l_si
        loss.backward()
        optimizer_cnn_h.step()

    print('[!] Epoch {} complete.'.format(epoch))

def output_img(output_dir):
    if not os.path.exists(output_dir):
        os.mkdir(output_dir)
    with torch.no_grad():
        for batch in test_data_loader:
            input, target, filename = batch[0].to(device), batch[1].to(device), batch[2]
            sr = cnn_h(input).cpu()
            for i in range(len(filename)):
                img = sr[i] * 128 + 127.5
                img = img.numpy().transpose(1, 2, 0)
                cv2.imwrite(output_dir + '/' + filename[i].split('/')[-1], img)

def save_model(model,filename):
    state = model.state_dict()
    for key in state: state[key] = state[key].clone().cpu()
    torch.save(state, filename)

def test_and_save(epoch):
    print('[!] Saving test results ... ', flush=True, end='')
    dir_name = options.result + '/output_' + str(epoch) + '_' + time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime())
    output_img(dir_name)
    print('done !', flush=True)
    evaluate(dir_name, options.train + '/valid_HR', options.train + '/valid_LR', cnn_r)
    evaluate(dir_name, options.train + '/valid_HR', options.train + '/valid_LR', cnn_r_train)

def checkpoint(epoch):
    cnn_h_out_path = options.model_output + '/cnn_h_epoch_{}'.format(epoch) + '_' + time.strftime("%Y-%m-%d_%H-%M-%S", time.localtime()) + '.pth'
    print('[!] Saving checkpoint into ' + cnn_h_out_path + ' ... ', flush=True, end='')
    save_model(cnn_h, cnn_h_out_path)

    print('done !', flush=True)

for epoch in range(1, options.epochs + 1):
    train(epoch)
    test_and_save(epoch)
    checkpoint(epoch)
