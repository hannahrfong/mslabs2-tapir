import comet_ml
import torch
import argparse
import yaml
import os
import logging

from configs.config import ExpConfig
from pytorch_lightning import Trainer, seed_everything
from pytorch_lightning.loggers import CometLogger
from utils.utils import dm_dict, model_dict
from utils.callback_utils import LoggingCallback
from evaluation.eval import IncrementalMetrics
from evaluation.benchmark import speed_benchmark
from pytorch_lightning.callbacks import ModelCheckpoint, EarlyStopping, \
                                        LearningRateMonitor

import optuna
from optuna.integration.pytorch_lightning import PyTorchLightningPruningCallback

def parse_args():
    """Parse input arguments"""
    parser = argparse.ArgumentParser(description='Experiment Args')

    parser.add_argument(
        '--RUN', dest='RUN_MODE',
        choices=['train', 'val', 'test'],
        help='{train, val, test}',
        type=str, required=True
    )

    parser.add_argument(
        '--MODEL_CONFIG', dest='MODEL_CONFIG',
        help='experiment configuration file',
        type=str, required=True
    )

    parser.add_argument(
        '--DATASET', dest='DATASET',
        choices=[
            'raddle-noisy-slot', 'snips-noisy-slot', 'atis-slot', 'snips-slot', 'multilingual', 'movie',
            'chunk-conll2003', 'pos-conll2003', 'ner-conll2003', 'pos-ud-ewt'],
        help='{raddle-noisy-slot, snips-noisy-slot, atis-slot, snips-slot, multilingual, movie, chunk-conll2003, pos-conll2003, \
            ner-conll2003, pos-ud-ewt}',
        type=str, required=True
    )

    parser.add_argument(
        '--SPLIT', dest='TRAIN_SPLIT',
        choices=['train', 'train+valid'],
        help='set training split',
        type=str
    )

    parser.add_argument(
        '--GPU', dest='GPU',
        help='select gpu, e.g. "0, 1, 2"',
        type=str
    )

    parser.add_argument(
        '--SEED', dest='SEED',
        help='fix random seed',
        type=int
    )

    parser.add_argument(
        '--VERSION', dest='VERSION',
        help='seed version control',
        type=str
    )

    parser.add_argument(
        '--RESUME', dest='RESUME',
        help='resume training',
        action='store_true',
    )

    parser.add_argument(
        '--PINM', dest='PIN_MEM',
        help='disable pin memory',
        action='store_false',
    )

    parser.add_argument(
        '--NW', dest='NUM_WORKERS',
        help='multithreaded loading to accelerate IO',
        default=4,
        type=int
    )

    parser.add_argument(
        '--CKPT_E', dest='CKPT_EPOCH',
        help='checkpoint epoch',
        type=int
    )

    parser.add_argument(
        '--CKPT_V', dest='CKPT_VERSION',
        help='checkpoint version',
        type=str
    )

    parser.add_argument(
        '--CKPT_PATH', dest='CKPT_PATH',
        help='load checkpoint path, if \
        possible use CKPT_VERSION and CKPT_EPOCH',
        type=str
    )

    parser.add_argument(
        '--DATA_ROOT_PATH', dest='DATA_ROOT_PATH',
        help='dataset root path',
        type=str
    )

    parser.add_argument(
        '--LOG_OFFLINE', dest='LOG_OFFLINE',
        help='Offline/online logging',
        action='store_true'
    )

    parser.add_argument(
        '--EXP_KEY', dest='EXP_KEY',
        help='comet experiment key, for resuming',
        type=str
    )

    parser.add_argument(
        '--INCR_EVAL', dest='INCR_EVAL',
        help='incremental evaluation',
        action='store_true'
    )

    parser.add_argument(
        '--SPD_BENCHMARK', dest='SPD_BENCHMARK',
        help='incremental inference speed benchmark',
        action='store_true'
    )

    args = parser.parse_args()
    return args


def main(cfgs):
    # Set seed for numpy, pytorch and python.random
    seed_everything(cfgs.SEED)
    metric = 'val_acc' if cfgs.DATASET not in cfgs.BIO_SCHEME else 'val_f1'
    cfgs_dict = cfgs.config_dict()
    exp_key = cfgs.EXP_KEY if hasattr(cfgs, 'EXP_KEY') else None

    comet_logger = CometLogger(
        api_key='',
        workspace='',
        project_name='',
        save_dir=cfgs.LOG_PATH,
        experiment_name='_'.join(
            [cfgs.MODEL, cfgs.VERSION, str(cfgs.SEED)]
        ),
        offline=cfgs.LOG_OFFLINE,
        experiment_key=exp_key
    )

    task = cfgs.TASK_TYPE
    model_task = '_'.join([cfgs.MODEL, task])

    if cfgs.MODEL == 'two-pass':
        task = task + '_revision'

    checkpoint_callback = ModelCheckpoint(
        monitor=metric,
        dirpath=os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET),
        filename='_'.join(
            [cfgs.DATASET, cfgs.MODEL, cfgs.VERSION, '{epoch}']
        ),
        mode='max'
    )

    early_stop_callback = EarlyStopping(
        monitor=metric,
        min_delta=0.0, 
        patience=cfgs.PATIENCE,
        mode='max'
    )

    lr_monitor = LearningRateMonitor(logging_interval='epoch')
    run_mode = cfgs.RUN_MODE

    if run_mode == 'train':
        if cfgs.RESUME:
            if cfgs.CKPT_PATH is not None:
                path = cfgs.CKPT_PATH
            else:
                path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                '_'.join([
                                    cfgs.DATASET, cfgs.MODEL,
                                    cfgs.CKPT_VERSION,
                                    'epoch=' + str(cfgs.CKPT_EPOCH) + '.ckpt'
                                ]))
        else:
            path = None

        datamodule = dm_dict[task](cfgs)
        datamodule.prepare_data()
        datamodule.setup()

        if cfgs.USE_GLOVE:
            pretrained_emb = datamodule.tokenizer.pretrained_emb
        else:
            pretrained_emb = None

        if cfgs.MODEL == 'two-pass':
            reviser_alias = '_'.join([cfgs.REVISER, cfgs.TASK_TYPE])
            reviser = model_dict[reviser_alias]['test']

            model = model_dict[model_task]['train'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx, reviser,
                pretrained_emb
            )

            if cfgs.REVISER_CKPT_PATH is not None:
                reviser_path = cfgs.REVISER_CKPT_PATH
            else:
                reviser_path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                    '_'.join([
                                        cfgs.DATASET, cfgs.REVISER,
                                        cfgs.REVISER_CKPT_VERSION,
                                        'epoch=' + str(cfgs.REVISER_CKPT_EPOCH) + '.ckpt'
                                    ]))

            # Load reviser weights
            reviser_ckpt = torch.load(reviser_path)
            model.reviser.load_state_dict(reviser_ckpt['state_dict'])

        else:
            model = model_dict[model_task]['train'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx,
                pretrained_emb
            )
            
        def objective(trial):
            cfgs.RNN_LAYER = trial.suggest_int('RNN_LAYER', 1, 4)
            cfgs.CTRL_LAYER = trial.suggest_int('CTRL_LAYER', 1, 2)
            cfgs.GRAD_CLIP = trial.suggest_categorical('GRAD_CLIP', [None, 0.5, 1.0])
            cfgs.LR = trial.suggest_categorical('LR', [5e-5, 7e-5, 1e-4, 1e-3])
            cfgs.BATCH_SIZE = trial.suggest_categorical('BATCH_SIZE', [16, 32, 64])
            cfgs.RNN_HIDDEN_SIZE = trial.suggest_categorical('RNN_HIDDEN_SIZE', [256, 512])
            cfgs.CTRL_HIDDEN_SIZE = trial.suggest_categorical('CTRL_HIDDEN_SIZE', [256, 512])
            cfgs.CACHE_SIZE = trial.suggest_categorical('CACHE_SIZE', [3, 5, 7])

            model = model_dict[model_task]['train'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx, reviser,
                pretrained_emb
            )

            trainer = Trainer(
                deterministic=True,
                max_epochs=cfgs.MAX_EPOCH,
                #devices=cfgs.GPU,
                devices='auto',
                accelerator='cpu',
                logger=comet_logger,
                callbacks=[early_stop_callback, LoggingCallback(cfgs_dict), checkpoint_callback,
                         lr_monitor],
                check_val_every_n_epoch=1,
                gradient_clip_val=cfgs.GRAD_CLIP,
                #resume_from_checkpoint=path,
                accumulate_grad_batches=cfgs.ACCU_GRAD
            )

            trainer.fit(model, datamodule=datamodule)

            val_f1 = trainer.callback_metrics["val_f1"].item()

            return val_f1

        pruner = optuna.pruners.MedianPruner() 
        
        study = optuna.create_study(direction="maximize")
        study.optimize(objective, n_trials=25)

        print("Best hyperparameters: ", study.best_trial.params)

    elif run_mode == 'val':
        if cfgs.CKPT_PATH is not None:
                path = cfgs.CKPT_PATH
        else:
            path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                '_'.join([
                                    cfgs.DATASET, cfgs.MODEL,
                                    cfgs.CKPT_VERSION,
                                    'epoch=' + str(cfgs.CKPT_EPOCH) + '.ckpt'
                                ]))

        datamodule = dm_dict[task](cfgs, valid=True)
        datamodule.prepare_data()
        datamodule.setup('test')

        if cfgs.USE_GLOVE:
            pretrained_emb = datamodule.tokenizer.pretrained_emb
        else:
            pretrained_emb = None

        if cfgs.MODEL == 'two-pass':
            reviser_alias = '_'.join([cfgs.REVISER, cfgs.TASK_TYPE])
            reviser = model_dict[reviser_alias]['test']

            model = model_dict[model_task]['test'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx, reviser,
                pretrained_emb
            )

            if cfgs.REVISER_CKPT_PATH is not None:
                reviser_path = cfgs.REVISER_CKPT_PATH
            else:
                reviser_path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                    '_'.join([
                                        cfgs.DATASET, cfgs.REVISER,
                                        cfgs.REVISER_CKPT_VERSION,
                                        'epoch=' + str(cfgs.REVISER_CKPT_EPOCH) + '.ckpt'
                                    ]))

            # Load reviser weights
            reviser_ckpt = torch.load(reviser_path)
            model.reviser.load_state_dict(reviser_ckpt['state_dict'])

        else:
            model = model_dict[model_task]['test'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx,
                pretrained_emb
            )

        ckpt = torch.load(path)
        model.load_state_dict(ckpt['state_dict'])

        trainer = Trainer(
            deterministic=True,
            max_epochs=1,
            gpus=cfgs.GPU,
            logger=comet_logger
        )

        if cfgs.INCR_EVAL:
            partial_outputs = IncrementalMetrics(
                cfgs, datamodule.test_dataloader(), model, datamodule.tokenizer.token2idx
            )
            partial_outputs.print_metrics(logger=comet_logger)
        elif cfgs.SPD_BENCHMARK:
            elapsed_time = speed_benchmark(
                cfgs, datamodule.test_dataloader(), model
            )
            cfgs.logger.info("Elapsed Time: {}".format(elapsed_time))
        else:
            trainer.test(model,
                         test_dataloaders=datamodule.test_dataloader(),
                         ckpt_path=path)

    elif run_mode == 'test':
        if cfgs.CKPT_PATH is not None:
                path = cfgs.CKPT_PATH
        else:
            path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                '_'.join([
                                    cfgs.DATASET, cfgs.MODEL,
                                    cfgs.CKPT_VERSION,
                                    'epoch=' + str(cfgs.CKPT_EPOCH) + '.ckpt'
                                ]))

        datamodule = dm_dict[task](cfgs)
        datamodule.prepare_data()
        datamodule.setup('test')

        if cfgs.USE_GLOVE:
            pretrained_emb = datamodule.tokenizer.pretrained_emb
        else:
            pretrained_emb = None

        if cfgs.MODEL == 'two-pass':
            reviser_alias = '_'.join([cfgs.REVISER, cfgs.TASK_TYPE])
            reviser = model_dict[reviser_alias]['test']

            model = model_dict[model_task]['test'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx, reviser,
                pretrained_emb
            )

            if cfgs.REVISER_CKPT_PATH is not None:
                reviser_path = cfgs.REVISER_CKPT_PATH
            else:
                reviser_path = os.path.join(cfgs.CKPTS_PATH, cfgs.DATASET,
                                    '_'.join([
                                        cfgs.DATASET, cfgs.REVISER,
                                        cfgs.REVISER_CKPT_VERSION,
                                        'epoch=' + str(cfgs.REVISER_CKPT_EPOCH) + '.ckpt'
                                    ]))

            # Load reviser weights
            reviser_ckpt = torch.load(reviser_path)
            model.reviser.load_state_dict(reviser_ckpt['state_dict'])

        else:
            model = model_dict[model_task]['test'](
                cfgs, datamodule.tokenizer.token2idx,
                datamodule.tokenizer.label2idx,
                pretrained_emb
            )

        ckpt = torch.load(path)
        model.load_state_dict(ckpt['state_dict'])

        trainer = Trainer(
            deterministic=True,
            max_epochs=1,
            #gpus=cfgs.GPU,
            logger=comet_logger
        )

        if cfgs.INCR_EVAL:
            partial_outputs = IncrementalMetrics(
                cfgs, datamodule.test_dataloader(), model, datamodule.tokenizer.token2idx
            )
            partial_outputs.print_metrics(logger=comet_logger)
        elif cfgs.SPD_BENCHMARK:
            elapsed_time = speed_benchmark(
                cfgs, datamodule.test_dataloader(), model
            )
            cfgs.logger.info("Elapsed Time: {}".format(elapsed_time))
        else:
            trainer.test(model,
                         dataloaders=datamodule.test_dataloader(),
                         ckpt_path=path)
    else:
        exit(-1)


if __name__ == "__main__":
    cfgs = ExpConfig()

    args = parse_args()
    args_dict = cfgs.parse_to_dict(args)

    path_cfg_file = './configs/path_config.yml'
    with open(path_cfg_file, 'r') as path_f:
        path_yaml = yaml.safe_load(path_f)

    model_cfg_file = './configs/{}.yml'.format(args.MODEL_CONFIG)
    with open(model_cfg_file, 'r') as model_f:
        model_yaml = yaml.safe_load(model_f)

    args_dict = {**args_dict, **model_yaml}

    cfgs.add_args(args_dict)
    cfgs.init_path(path_yaml)

    logging.basicConfig(level=logging.INFO,
                        filemode='w',
                        filename=os.path.join(cfgs.LOG_PATH,
                                              cfgs.RUN_MODE + '_' + cfgs.VERSION + '.log'),
                        format='%(asctime)s %(name)-12s %(levelname)-8s %(message)s',
                        datefmt='%m-%d %H:%M',
                        )

    cfgs.setup()

    cfgs.logger.info("Hyperparameters:")
    cfgs.logger.info(cfgs)

    cfgs.check_path(dataset=args.DATASET)
    main(cfgs)
