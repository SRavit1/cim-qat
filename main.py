import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
import torch.backends.cudnn as cudnn
import torchvision
import torchvision.transforms as transforms
import os
import argparse
from models import *
from utils import progress_bar
import logger
import cim_utils
import yaml
from pathlib import Path
from load_config import load_config
import sys

config_path = sys.argv[1] if len(sys.argv) >= 2 else None
args = load_config(config_path=config_path)
cim_utils.initialize_params(config_path)

torch.autograd.set_detect_anomaly(True)
logger = logger.Logger(args.log_dir)
device = 'cuda' if torch.cuda.is_available() else 'cpu'
start_epoch = 0  # start from epoch 0 or last checkpoint epoch

FLOAT_CKPT_PATH = "./" + "_".join([args.net_name, "float"]) + ".pth"
NONFUSED_QUANT_CKPT_PATH = "./" + "_".join([args.net_name, "quant", args.clamping_range_mode, args.cim_signed_type, "nonfused_bn"]) + ".pth"
QUANT_CKPT_PATH = "./" + "_".join([args.net_name, "quant", args.clamping_range_mode, args.cim_signed_type, "fused_bn" if args.fuse_bn else "nonfused_bn"]) + ".pth"
CKPT_PATH = FLOAT_CKPT_PATH

transform_train = transforms.Compose([
    transforms.RandomCrop(32, padding=4),
    transforms.RandomHorizontalFlip(),
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])
transform_test = transforms.Compose([
    transforms.ToTensor(),
    transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
])

trainset = torchvision.datasets.CIFAR10(
    root='./data', train=True, download=True, transform=transform_train)
trainloader = torch.utils.data.DataLoader(
    trainset, batch_size=128, shuffle=True, num_workers=2)
testset = torchvision.datasets.CIFAR10(
    root='./data', train=False, download=True, transform=transform_test)
testloader = torch.utils.data.DataLoader(
    testset, batch_size=100, shuffle=False, num_workers=2)

classes = ('plane', 'car', 'bird', 'cat', 'deer',
           'dog', 'frog', 'horse', 'ship', 'truck')
net_dict = {
    'VGG': VGG,
    'ResNet18': ResNet18,
    'PreActResNet18': PreActResNet18,
    'GoogLeNet': GoogLeNet,
    'DenseNet121': DenseNet121,
    'ResNeXt29_2x64d': ResNeXt29_2x64d,
    'MobileNet': MobileNet,
    'MobileNetV2': MobileNetV2,
    'DPN92': DPN92,
    'ShuffleNetG2': ShuffleNetG2,
    'SENet18': SENet18,
    'ShuffleNetV2': ShuffleNetV2,
    'EfficientNetB0': EfficientNetB0,
    'RegNetX_200MF': RegNetX_200MF,
    'SimpleDLA': SimpleDLA
}

def observe_data():
    # since we're not training, we don't need to calculate the gradients for our outputs
    with torch.no_grad():
        for i, data in enumerate(trainloader):
            images, labels = data
            images, labels = images.to(device), labels.to(device)
            # calculate outputs by running images through the network
            outputs = net(images)

def train(epoch):
    print('\nEpoch: %d' % epoch)
    net.train()
    train_loss = 0
    correct = 0
    total = 0
    for batch_idx, (inputs, targets) in enumerate(trainloader):
        inputs, targets = inputs.to(device), targets.to(device)
        optimizer.zero_grad()
        outputs = net(inputs)
        loss = criterion(outputs, targets)
        loss.backward()
        if cim_utils.conv_mode in ["quantize", "adc_quantize"]:
            for p in net.modules():
                if isinstance(p, nn.Conv2d):
                    p.weight.data.copy_(p.weight_org)
        optimizer.step()
        if cim_utils.conv_mode in ["quantize", "adc_quantize"]:
            for p in net.modules():
                if isinstance(p, nn.Conv2d):
                    p.weight_org.data.copy_(p.weight.data)

        train_loss += loss.item()
        _, predicted = outputs.max(1)
        total += targets.size(0)
        correct += predicted.eq(targets).sum().item()

        progress_bar(batch_idx, len(trainloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                     % (train_loss/(batch_idx+1), 100.*correct/total, correct, total))
    acc = 100.*correct/total
    return acc

def test(epoch):
    net.eval()
    test_loss = 0
    correct = 0
    total = 0
    with torch.no_grad():
        for batch_idx, (inputs, targets) in enumerate(testloader):
            inputs, targets = inputs.to(device), targets.to(device)
            if cim_utils.conv_mode in ["quantize", "adc_quantize"]:
                for p in net.modules():
                    if hasattr(p, "weight_org"):
                        p.weight.data.copy_(p.weight_org)
            outputs = net(inputs)
            loss = criterion(outputs, targets)

            test_loss += loss.item()
            _, predicted = outputs.max(1)
            total += targets.size(0)
            correct += predicted.eq(targets).sum().item()

            progress_bar(batch_idx, len(testloader), 'Loss: %.3f | Acc: %.3f%% (%d/%d)'
                         % (test_loss/(batch_idx+1), 100.*correct/total, correct, total))

    acc = 100.*correct/total
    return acc

def run_training():
    best_train_acc = 0
    best_test_acc = 0
    n_epochs = args.float_train_epochs if cim_utils.conv_mode=="float" else args.quant_train_epochs
    for epoch in range(0, n_epochs):
        train_acc = train(epoch)
        test_acc = test(epoch)
        scheduler.step()
        if train_acc > best_train_acc:
            best_train_acc = train_acc
        if test_acc > best_test_acc:
            best_test_acc = test_acc
            torch.save(net.state_dict(), CKPT_PATH)
    return best_train_acc, best_test_acc

logger.log("INSTANTIATING " + args.net_name)
net = net_dict[args.net_name]()
net = net.to(device)
criterion = nn.CrossEntropyLoss()
optimizer = optim.SGD(net.parameters(), lr=args.lr, momentum=0.9, weight_decay=5e-4)
scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer, T_max=200)

# Initialize gain value in ADC_Conv2d layers
gain = torch.nn.parameter.Parameter(torch.Tensor([1.]).to(device)) if args.clamping_range_mode == "per_model" else None
for p in net.modules():
    if isinstance(p, cim_utils.ADC_Conv2d):
        if args.clamping_range_mode == "per_layer":
            gain = torch.nn.parameter.Parameter(torch.Tensor([1.]).to(device))
        p.gain = gain
        p.gain_enabled = True

# Load & pretrain floating point network
cim_utils.conv_mode = "float"
if os.path.exists(FLOAT_CKPT_PATH):
    logger.log("LOADING PRETRAINED FLOAT WEIGHTS")
    net.load_state_dict(torch.load(FLOAT_CKPT_PATH), strict=False)
else:
    logger.log("PRETRAINING FLOATING NETWORK")
    CKPT_PATH = FLOAT_CKPT_PATH
    train_accuracy, test_accuracy = run_training()

# Test floating point network
test_accuracy = test(0)
logger.log("FLOAT TESTING ACCURACY IS: " + str(test_accuracy))

# Calculate quantization parameters
for p in net.modules():
    if isinstance(p, cim_utils.ADC_Conv2d):
        p.calculate_qparams()

# Load pretrained quantized network if it exists
cim_utils.conv_mode = "quantize"
if os.path.exists(QUANT_CKPT_PATH):
    logger.log("LOADING PRETRAINED QUANTIZED NETWORK")
    net.load_state_dict(torch.load(QUANT_CKPT_PATH), strict=False)

    if args.fuse_bn:
        print("DEACTIVATING BATCHNORM")
        for p in net.modules():
            if isinstance(p, cim_utils.ADC_Conv2d):
                p.bn.deactivated = True
else:
    # Fuse batchnorm
    logger.log("FUSING BATCHNORM")
    if args.fuse_bn:
        if not os.path.exists(NONFUSED_QUANT_CKPT_PATH):
            raise Exception
        net.load_state_dict(torch.load(NONFUSED_QUANT_CKPT_PATH), strict=False)
        #print("Stage 1", test(0))
        for p in net.modules():
            if isinstance(p, cim_utils.ADC_Conv2d):
                p.fuse_batchnorm()
        #print("Stage 2", test(0))

    # Train quantized network
    CKPT_PATH = QUANT_CKPT_PATH
    cim_utils.conv_mode = "adc_quantize"
    cim_utils.clamping = False
    logger.log("TRAINING QUANTIZED NETWORK")
    best_train_acc, best_test_acc = run_training()
    logger.log("BEST QUANTIZED TESTING ACCURACY IS: " + str(best_test_acc))

# Test quantized network without clamping
cim_utils.conv_mode = "adc_quantize"
cim_utils.clamping = False
test_acc = test(0)
logger.log("BEST QUANTIZED TESTING ACCURACY (No clamping) IS: " + str(test_acc))

# Test quantized network with clamping
cim_utils.clamping = True
test_acc = test(0)
logger.log("BEST QUANTIZED TESTING ACCURACY (Clamping) IS: " + str(test_acc))