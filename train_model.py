import torch
from torch import nn
import numpy as np
from datetime import datetime

import os
from tqdm import tqdm
import argparse

from data import get_train_validation_set, get_test_set
from model import FairClassifier
from evaluation import *
from torch.utils.tensorboard import SummaryWriter

# tokenizer = torch.hub.load('huggingface/pytorch-transformers', 'tokenizer', 'bert-base-uncased', return_dict=False)    # Download vocabulary from S3 and cache.
def bert_collate(data_batch):
    x, t, d = [], [], []
    for modality, target, attribute in data_batch:
        x.append(modality)
        t.append(target)
        d.append(attribute)

    bert_input = tokenizer(x, padding=True, truncation=True, return_tensors='pt')
    return bert_input, torch.Tensor([t]).T, torch.Tensor([d]).T

def set_seed(seed: int):
    """
    Function for setting the seed for reproducibility.
    """
    np.random.seed(seed)
    torch.manual_seed(seed)
    if torch.cuda.is_available():
        torch.cuda.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.determinstic = True
    torch.backends.cudnn.benchmark = False

def get_optimizer(parameters, lr: float, optimizer: str) -> torch.optim.Optimizer:
    """Returns the specified optimizer with the given parameters and learning rate.

    Args:
        parameters ([type]): the parameters the optimizer should optimize.
        lr (float): the learning rate to use.
        optimizer (str): the optimizer to use.

    Returns:
        torch.optim.Optimizer: the optimizer object.
    """
    optimizer = optimizer.lower()
    if optimizer == "sgd":
        opt = torch.optim.SGD(parameters, lr)
    elif optimizer == "adam":
        opt = torch.optim.Adam(parameters, lr)
    else:
        ValueError("The optimizer {} is not implemented.".format(optimizer))
    return opt

def name_model(dataset: str, attribute: str, lr_f: float, lr_g: float, lr_j: float, lmbda: float, optim: str, seed: int) -> str:
    """Parse the training arguments into a filename 
    Returns:
        str: The name to use for logging and saving
    """
    # We are using the log10 to indicate the learning rate for learning rates ending in a 1
    lrs = [str(np.log10(lr))[:2] if int(str(lr)[-1]) == 1 else str(lr)[1:] for lr in [lr_f, lr_g, lr_j]]
    model = "{}_{}_{}{}{}_{}_{}_{}".format(dataset, attribute, *lrs, lmbda, optim, str(seed))
    date = datetime.now().strftime("%b%d-%H:%M")
    return os.path.join(model, date)

def train_model(model: nn.Module, train_loader: torch.utils.data.DataLoader, val_loader: torch.utils.data.DataLoader,
                optimizer:str, lr_f: float, lr_g: float, lr_j: float, lmbda: float, epochs: int, checkpoint_name: str, 
                device: torch.device, progress_bar: bool, writer: torch.utils.tensorboard.SummaryWriter) -> nn.Module:
    """
    Trains a given model architecture for the specified hyperparameters.

    Args:
        model: Model architecture to train.
        dataset: Specifiec dataset.
        lr: Learning rate to use in the optimizer.
        batch_size: Batch size to train the model with.
        epochs: Number of epochs to train the model for.
        checkpoint_name: Filename to save the best model on validation to.
        device: Device to use for training.
    Returns:
        model: Model that has performed best on the validation set.
    """

    # Initialize the optimizer and loss function
    group_specific_params = [param for gsm in model.group_specific_models for param in gsm.parameters()]
    feature_extractor_params = model.featurizer.parameters()
    joint_classifier_params = model.joint_classifier.parameters()

    group_specific_optimizer = get_optimizer(group_specific_params, lr=lr_g, optimizer=optimizer)
    feature_extractor_optimizer = get_optimizer(feature_extractor_params, lr=lr_f, optimizer=optimizer)
    joint_classifier_optimizer = get_optimizer(joint_classifier_params, lr=lr_j, optimizer=optimizer)

    loss_module = nn.BCELoss()

    # Training loop with validation after each epoch. Save the best model, and remember to use the lr scheduler.
    for epoch in tqdm(range(epochs), position=0, desc="epoch", disable=progress_bar):
        model.train()
        nr_batches = len(train_loader)

        # Group specific training
        if lmbda:
            group_correct, group_total = 0, 0
            group_loss = 0
            for i, (x, t, d) in enumerate(tqdm(train_loader, position=1, desc="group", leave=False, disable=progress_bar)):
                x = x.to(device)
                t = t.to(device)
                d = d.to(device)
                group_specific_optimizer.zero_grad()
                pred_group_spe = model.group_forward(x, d)

                L_D = loss_module(pred_group_spe, t.squeeze())
                L_D.backward()

                group_specific_optimizer.step()

                group_correct += num_correct_predictions(pred_group_spe, t)
                group_total += len(x)
                group_loss += L_D

                writer.add_scalar("train/batch/L_D", L_D, i + epoch * nr_batches)

            writer.add_scalar("train/L_D", group_loss, epoch)
            writer.add_scalar("train/group_acc", group_correct / group_total, epoch)

        # Feature extractor and joint classifier trainer
        joint_correct, joint_total = 0, 0
        L_0_total, L_R_total = 0, 0
        for i, (x, t, d) in enumerate(tqdm(train_loader, position=1, desc="joint", leave=False, disable=progress_bar)):
            x = x.to(device)
            t = t.to(device)
            d = d.to(device)
            # Sample d values for the group agnostic model
            d_tilde = train_loader.dataset.sample_d(d.shape)

            # Get model predictions
            pred_joint, pred_group_spe, pred_group_agn = model.forward(x, d, d_tilde)

            # Calculate L_0 and L_R (group agnostic and specific are flipped because of the negative sign in BCELoss
            # and because if this sign goes in front of Eq. 17, the losses should be flipped)
            L_R = lmbda * (loss_module(pred_group_agn, t) - loss_module(pred_group_spe, t))

            # Add L_R to the feature extractor gradients (but not to the joint classifier)
            feature_extractor_optimizer.zero_grad()
            group_specific_optimizer.zero_grad()
            L_R.backward(retain_graph=True)
            joint_classifier_optimizer.zero_grad()
            
            # Add L_0 to both the feature extractor and joint classifier gradients
            L_0 = loss_module(pred_joint, t)
            L_0.backward()

            # Update the classifier and feature extractor
            joint_classifier_optimizer.step()
            feature_extractor_optimizer.step()
            
            joint_correct += num_correct_predictions(pred_joint, t)
            joint_total += len(x)
            
            L_0_total += L_0
            L_R_total += L_R

            writer.add_scalar("train/batch/L_0", L_0, i + epoch * nr_batches)
            writer.add_scalar("train/batch/L_R", L_R, i + epoch * nr_batches)
        
        writer.add_scalar("train/joint_acc", joint_correct / joint_total, epoch)
        writer.add_scalar("train/L_0", L_0_total, epoch)
        writer.add_scalar("train/L_R", L_R_total, epoch)
        
        if val_loader and epoch % 2 == 0:
            predictions = []
            targets = []
            with torch.no_grad():
                model.eval()

                for x, t, d in tqdm(val_loader, desc="val", leave=False, disable=progress_bar):
                    x = x.to(device)
                    t = t.to(device)
                    d = d

                    p, _, _ = model.forward(x)

                    p = p.cpu().unsqueeze(dim=-1)
                    t = t.cpu().unsqueeze(dim=-1)
                    d = d.cpu()

                    # Save predictions and targets for further evaluation
                    predictions.append(p)
                    targets.append(t)

            predictions = torch.cat(predictions)
            targets = torch.cat(targets)

            val_acc = num_correct_predictions(predictions, targets) / len(predictions)
            writer.add_scalar("val/acc", val_acc, epoch)
    
    # Save best model and return it.
    torch.save(model.state_dict(), os.path.join("runs", checkpoint_name))
    return model

def num_correct_predictions(predictions: torch.Tensor, targets: torch.Tensor) -> int:
    pred = (predictions > 0.5).long()
    correct = (pred == targets).sum()
    return correct.item()

def test_model(model: nn.Module, test_loader: torch.utils.data.DataLoader, device: torch.device, seed: int, progress_bar: bool) -> float:
    """
    Tests a trained model on the test set.

    Args:
        model: Model architecture to test.
        dataset: Specify dataset where test_set is loaded from.
        batch_size: Batch size to use in the test.
        device: Device to use for training.
        seed: The seed to set before testing to ensure a reproducible test.
    Returns:
        test_results: The average accuracy on the test set (independent of the attribute).
    """

    set_seed(seed)

    # Get the model predictions
    predictions = []
    targets = []
    attributes = []
    with torch.no_grad():
        model.eval()
        for x, t, d in tqdm(test_loader, desc="test", disable=progress_bar):
            x = x.to(device)
            t = t.to(device).squeeze()
            d = d

            p, _, _ = model.forward(x)

            p = p.cpu().unsqueeze(dim=-1)
            t = t.cpu().unsqueeze(dim=-1)
            d = d.cpu()

            # Save predictions and targets for further evaluation
            predictions.append(p)
            targets.append(t)
            attributes.append(d)

    predictions = torch.cat(predictions)
    targets = torch.cat(targets)
    attributes = torch.cat(attributes)

    test_acc = num_correct_predictions(predictions, targets) / len(predictions)

    # Compute overal margin and AUC statistics
    area_under_curve, area_between_curves_val, M_group, A_group, C_group, P_A_group, P_C_group = evalutaion_statistics(predictions, targets, attributes)

    margin_plot = plot_margin_group(M_group)
    precision_plot = accuracy_coverage_plot(P_A_group, P_C_group, 'precision')
    ac_plot = accuracy_coverage_plot(A_group, C_group, 'accuracy')

    return test_acc,area_under_curve, area_between_curves_val, margin_plot, precision_plot, ac_plot

def main(checkpoint: str, dataset: str, attribute: str, num_workers: int, optimizer: str,lr_f: float, lr_g: float, lr_j: float, lmbda: float,
        batch_size: int, epochs: int, seed: int, dataset_root:str, progress_bar: bool):
    """
    Function that summarizes the training and testing of a model.

    Args:
        dataset: Dataset to test.
        batch_size: Batch size to use in the test.
        device: Device to use for training.
        seed: The seed to set before testing to ensure a reproducible test.
    Returns:
        test_results: Dictionary containing an overview of the accuracies achieved on the different
                      corruption functions and the plain test set.
    """
    device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    torch.multiprocessing.set_sharing_strategy('file_system')
    collate_fn = bert_collate if dataset == "civil" else None
    set_seed(seed)

    print("Training on ", device)

    # Check if the given configuration has been trained before
    if checkpoint:
        checkpoint_name = os.path.split(checkpoint)[-1]
        checkpoint_path = checkpoint
    else:
        checkpoint_name = name_model(dataset, attribute, lr_f, lr_g, lr_j, lmbda, optimizer, seed) + '.pt'
        checkpoint_path = os.path.join("runs", checkpoint_name)

    writer = SummaryWriter(log_dir=os.path.join("runs", checkpoint_name[:-3]))
    hparams = {"data": dataset, "attr": attribute, "opt": optimizer, "lr_f": lr_f, "lr_g": lr_g, "lr_j": lr_j, "seed": seed, "lambda": lmbda}

    if os.path.exists(checkpoint_path):
        # Create dummy model and load the trained model from disk
        print("Found model", checkpoint_path)
        model = FairClassifier(dataset, nr_attr_values=10).to(device)
        model.load_state_dict(torch.load(checkpoint_path, map_location=device), strict=False)
        model.to(device)
    else:
        # Load the dataset with the given parameters, initialize the model and start training
        writer = SummaryWriter(log_dir=os.path.join("runs", checkpoint_name[:-3]))
        train_set, val_set = get_train_validation_set(dataset, root=dataset_root, attribute=attribute)
        train_loader = torch.utils.data.DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=num_workers, collate_fn=collate_fn, drop_last=True)
        val_loader = torch.utils.data.DataLoader(train_set, batch_size=batch_size, shuffle=True, num_workers=num_workers, collate_fn=collate_fn) if val_set else None

        model = FairClassifier(dataset, nr_attr_values=train_set.nr_attr_values()).to(device)
        model = train_model(model, train_loader, val_loader, optimizer, lr_f, lr_g, lr_j, lmbda, epochs,
                            checkpoint_name, device, progress_bar, writer)
        writer.close()
    
    writer = SummaryWriter(log_dir=os.path.join("runs_eval", checkpoint_name[:-3]))
    test_set = get_test_set(dataset, dataset_root)
    test_loader = torch.utils.data.DataLoader(test_set, batch_size=batch_size, num_workers=num_workers)
    test_acc, area_under_curve, area_between_curves_val, margin_plot, precision_plot, ac_plot = test_model(model, test_loader, device, seed, progress_bar)

    writer.add_hparams(hparams, {"acc": test_acc, "auc": area_under_curve, "abc": area_between_curves_val}) 
    writer.add_figure('margin', margin_plot)
    writer.add_figure('precision', precision_plot)
    writer.add_figure('accuracy', ac_plot)
    writer.close()

if __name__ == '__main__':
    # Command line arguments
    parser = argparse.ArgumentParser()
    
    # General hyperparameters
    parser.add_argument('--checkpoint', default='', type=str,
                        help='A filename in the models directory which you want to evaluate. \
                        If not found will train a model with this name.')
    parser.add_argument('--dataset', default='adult', type=str,
                        help='Name of the dataset to evaluate on.')
    parser.add_argument('--attribute', default="", type=str,
                        help='The sensitive attribute to use during training. \
                            If empty the default dataset specific attribute will be used.')
    parser.add_argument('--num_workers', default=3, type=int,
                        help='The amount of threads for the data loader object.')
    
    # Optimizer hyperparameters
    parser.add_argument('--optimizer', default="adam", type=str, choices=["sgd", "adam"],
                        help='The optimizer to use. Available options are: sgd, adam')
    parser.add_argument('--lr_f', default=0.001, type=float,
                        help='Learning rate to use for the featurizer')
    parser.add_argument('--lr_g', default=0.001, type=float,
                        help='Learning rate to use for the group specific models')
    parser.add_argument('--lr_j', default=0.001, type=float,
                        help='Learning rate to use for the joint classifier.')
    parser.add_argument('--lmbda', default=0.7, type=float,
                        help='The factor to multiply the regularization loss with, before backpropagating.')
    parser.add_argument('--batch_size', default=32, type=int,
                        help='Minibatch size.')

    # Other hyperparameters
    parser.add_argument('--epochs', default=20, type=int,
                        help='Max number of epochs.')
    parser.add_argument('--seed', default=42, type=int,
                        help='Seed to use for reproducing results.')

    # Other arguments
    parser.add_argument('--dataset_root', default="data", type=str,
                        help="the root of the data folders.")
    parser.add_argument('--progress_bar', action="store_true",
                        help="Turn progress bar on.")

    args = parser.parse_args()
    kwargs = vars(args)

    print("Training with: ", str(kwargs))

    main(**kwargs)
        