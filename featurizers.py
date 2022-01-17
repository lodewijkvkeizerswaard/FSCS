import torch
import torch.nn as nn
import torchvision.models as models

ADULT_DATASET_FEATURE_SIZE = 105
NODE_SIZE = 80


def get_model(dataset_name: str):
    """
    Returns the model architecture for the provided dataset_name. 
    """
    if dataset_name == 'adult':
        model = AdultFeaturizer()
        out_features = NODE_SIZE

    elif dataset_name == 'celeba':
        model = CelebAFeaturizer()
        out_features = 2048

    elif dataset_name == 'civilcomments':
        model = CivilCommentsFeaturizer()
        out_features = NODE_SIZE

    elif dataset_name == 'chexpert':
        model = CheXPertFeaturizer()
        out_features = 1024
    else:
        assert False, f'Unknown network architecture \"{dataset_name}\"'

    return out_features, model


def drop_classification_layer(model):
    return torch.nn.Sequential(*(list(model.children())[:-1]))


class AdultFeaturizer(nn.Module):
    def __init__(self):
        self.model = nn.Sequential(
            nn.Linear(ADULT_DATASET_FEATURE_SIZE, NODE_SIZE),
            nn.SELU()
        )

    def forward(self, x):
        return self.model(x)


class CelebAFeaturizer(nn.Module):
    def __init__(self):
        self.model = models.resnet50(pretrained=True)

    def forward(self, x):
        return self.model(x)


class CivilCommentsFeaturizer(nn.Module):
    def __init__(self):
        bert_model = torch.hub.load(
            'huggingface/pytorch-transformers', 'model', 'bert-base-uncased')

        fc_model = nn.Sequential(
            nn.Linear(1024, NODE_SIZE),
            nn.SELU()
        )
        self.model = torch.nn.Sequential(bert_model, fc_model)

    def forward(self, x):
        return self.model(x)


class CheXPertFeaturizer(nn.Module):
    def __init__(self):
        model = models.densenet121(pretrained=True)
        model = drop_classification_layer(model)
        self.model = nn.Sequential(model, nn.AdaptiveAvgPool2d((1, 1)))

    def forward(self, x):
        return self.model(x)
