import matplotlib
import torch
import numpy as np
import matplotlib.pyplot as plt
from sklearn.metrics import auc

import os 
from train_model import test_model, set_seed, get_test_set
from model import FairClassifier

def confidence_score(x: torch.Tensor) -> torch.Tensor:
    return 0.5 * np.log(x / (1 - x))

def split(x: torch.Tensor, d: torch.Tensor):
    # Groups x samples based on d-values
    sorter = torch.argsort(d, dim=0)
    _, counts = torch.unique(d, return_counts=True)
    return torch.split(x[sorter, :], counts.tolist()), torch.split(d[sorter], counts.tolist())

def margin(prediction: torch.Tensor, target: torch.Tensor) -> torch.Tensor:
    """
    Compares the prediction and the target for the calculation of the margin 
    for a datapoint.
    Args:
        prediction: The predictions of the samples.
        target: The corresponding correct targets for the prediction.
    Returns:
        margin: The margin values for the corresponding samples.
    """
    pred = prediction.clone()
    correct = (torch.round(pred) == target).type(torch.int) * 2 - 1
    pred[pred<0.5] = 1 - pred[pred<0.5]
    margin = correct * confidence_score(pred)
    # Cap large values to 20
    margin[margin > 20] = 20
    margin[margin < - 20] = -20
    return margin

def margin_group(predictictions: torch.Tensor, targets: torch.Tensor, attributes: torch.Tensor) -> dict:
    """This function splits the predictions and targets on group assignment and calculates the corresponding
        group specific margin. """
    pred_split, d_split = split(predictictions, attributes)
    tar_split, _ = split(targets, attributes)
    margins = {int(d[0]):margin(p, t) for p, t, d in zip(pred_split, tar_split, d_split)}
    return margins

def precision_group(predictictions: torch.Tensor, targets: torch.Tensor, attributes: torch.Tensor) -> dict:
    """Calculate the group specific precisions."""
    pred_split, d_split = split(predictictions, attributes)
    tar_split, _ = split(targets, attributes)
    margin_precision = {}
    for group_pred, group_tar, group_attr in zip(pred_split, tar_split, d_split):
        pred_round = torch.round(group_pred)
        zero_index = (pred_round == 0).nonzero()[:,0]
        group_pred_y_hat_1 = group_pred[~zero_index,:]
        group_tar_y_hat_1 = group_tar[~zero_index,:]
        margin_precision[group_attr[0].item()] = margin(group_pred_y_hat_1, group_tar_y_hat_1)
    return margin_precision

def evalutaion_statistics(predictions: torch.Tensor, targets: torch.Tensor, attributes: torch.Tensor):
    """
    Computes the evaluation statistics for the test data.
    Args:
        predictions: The predictions of the samples.
        targets: The corresponding targets for the predictions.
        attributes: The corresponding attributes for the predictiosn.
    Returns:
        area_under_curve: The area under the accuracy-coverage curve.
        area_between_curves_val: The area between the precision-coverage curves.
        M_group: The margin values of the samples per group.
        A_group: The accuracies for different values of tau per group.
        C_group: The corresponding coverages for different values of tau per group.
        P_A_group: The precision values for different values of tau per group.
        P_C_group: The corresponding coverages for different values of tau per group.
        """
    M = margin(predictions, targets) 
    max_tau = torch.max(torch.abs(M)).item()
    taus = np.arange(0, max_tau, step=0.001)

    # Compute overal margin and AUC statistics
    CDF = lambda margin, tau: (len(margin[margin <= tau]) / len(margin))
    CDF_correct = lambda margin, tau: 1 - CDF(margin, tau)
    CDF_covered = lambda margin, tau: CDF(margin, -tau) + 1 - CDF(margin, tau)

    A = [CDF_correct(M, tau) / CDF_covered(M, tau) if CDF_covered(M, tau) > 0 else 1 for tau in taus]
    C = [CDF_covered(M, tau) for tau in taus]
    area_under_curve = auc(C, A)

    # Compute group specific margins and accuracies
    M_group = margin_group(predictions, targets, attributes)
    A_group = {group_key: [CDF_correct(group_margin, tau) / CDF_covered(group_margin, tau) if CDF_covered(group_margin, tau) > 0 else 1 for tau in taus] for group_key, group_margin in M_group.items()}
    C_group = {group_key: [CDF_covered(group_margin, tau) for tau in taus] for group_key, group_margin in M_group.items()}

    # Compute the group specific precisions for Y_hat = 1
    P_M_group = precision_group(predictions, targets, attributes)
    P_A_group = {group_key: [CDF_correct(group_margin, tau) / CDF_covered(group_margin, tau) if CDF_covered(group_margin, tau) > 0 else 1 for tau in taus] for group_key, group_margin in P_M_group.items()}
    P_C_group = {group_key: [CDF_covered(group_margin, tau) for tau in taus] for group_key, group_margin in P_M_group.items()}

    area_between_curves = abc(P_A_group, P_C_group)
    
    return area_under_curve, area_between_curves, M_group, A_group, C_group, P_A_group, P_C_group

def plot_margin_group(margins: dict) -> matplotlib.figure.Figure:
    """
    Plots the margin distributions for two groups.
    Args:
        margins: A dictionary containing the margins for all groups with label `g` and margins `m`.
    Returns: 
        A matplotlib histogram figure with the margin for each group.
    """
    fig, ax = plt.subplots(1, 1, tight_layout=True)
    for g, m in margins.items():
        ax.hist(m.numpy().flatten(), bins='auto', density=True, alpha=0.5, label='Group ' + str(g))
    ax.set_xlabel('k (x)')
    ax.legend(loc="upper left")
    return fig

def abc(precisions: dict, coverages:dict) -> float:
    """
    Calculates the area between two curves.
    Args:
        precisions: The precision values for the two curves.
    Returns:
        area: The area between the two curves.
    """
    for group in coverages:
        coverages[group] = [round(i,3) for i in coverages[group]]
    
    final_coverages, final_precisions0, final_precisions1 = [], [], []
    for value in coverages[0]:
        if value in coverages[1] and value not in final_coverages:
            final_coverages.append(value)
            index_0 = coverages[0].index(value)
            index_1 = coverages[1].index(value)
            final_precisions0.append(precisions[0][index_0])
            final_precisions1.append(precisions[1][index_1])
   
    area = 0
    for x, y in zip(final_precisions0, final_precisions1):
        area += abs(x - y)
    return area/len(final_precisions0)


def accuracy_coverage_plot(accuracies: dict, coverages: dict, ylabel: str) -> matplotlib.figure.Figure:
    """
    Plots the accuracy vs. the coverage.
    Args:
        accuracies: Dict of accuracies split on group/attribute and depending on different values of tau.
        coverages: The corresponding coverages for the accuracies.
    """
    fig = plt.figure()
    for group in accuracies.keys():
        coverages[group].reverse()
        accuracies[group].reverse()
        plt.plot(coverages[group], accuracies[group], label="Group " + str(int(group)))
    plt.xlabel('coverage')
    plt.ylabel(ylabel)
    plt.ylim([0.4, 1.01])
    plt.xlim([0.15, 1.0])
    plt.legend(loc="lower left")
    plt.title("Group-specific "+ ylabel+"-coverage curves.")
    return fig

def evaluate(dataset, lmbda, checkpoint="", verbose=False):
    """
    Runs tests for a dataset and given lambda for all present seeds

    :params:
    lmbda: specify lambda value used during training (directory has to be present)
    verbose: specify if results, including images, should be outputted per seed
    """
    device = torch.device("cuda:0") if torch.cuda.is_available() else torch.device("cpu")
    acc_scores, auc_scores, abc_scores = [], [], []
    if checkpoint != "":
        path=checkpoint
    else:
        path = os.path.join(*['models', dataset, str(lmbda)])

    for model_name in os.listdir(path):
        seed = int(os.path.splitext(model_name)[0])
        model = FairClassifier(dataset).to(device)
        model.load_state_dict(torch.load(os.path.join(path, model_name), map_location=device), strict=False)

        test_set = get_test_set(dataset)
        test_loader = torch.utils.data.DataLoader(test_set, batch_size=BATCH_SIZE, num_workers=NUM_WORKERS)

        test_acc_score, area_under_curve, area_between_curves_val, \
        margin_plot, precision_plot, ac_plot = test_model(model, test_loader, device, seed, progress_bar=True)

        acc_scores.append(test_acc_score)
        auc_scores.append(area_under_curve)
        abc_scores.append(area_between_curves_val)

        if verbose:
            plt.show()
            print("Seed:", seed)
            print("Test Accuracy:", test_acc_score)
            print("Area Under Curve:", area_under_curve)
            print("Area Between Curve:", area_between_curves_val)

    acc_scores = np.array(acc_scores)
    auc_scores = np.array(auc_scores)
    abc_scores = np.array(abc_scores)

    if verbose:
        print("-------------------------------")
    print("Mean Test Accuracy:", acc_scores.mean(), "std:", acc_scores.std())
    print("Mean Area Under Curve:", auc_scores.mean(), "std:", auc_scores.std())
    print("Mean Area Between Curve:", abc_scores.mean(), "std:", abc_scores.std())

