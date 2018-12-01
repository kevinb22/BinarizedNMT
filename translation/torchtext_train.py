import torch
import pickle
import argparse
import os
from tqdm import trange, tqdm
import torch
import torchtext
from torchtext import data
from torchtext import datasets
from torch import nn
import torch.nn.functional as F
import math

from models import SimpleLSTMModel, AttentionRNN
from train_args import get_arg_parser
import constants
from vocab import Vocabulary, load_vocab
import dataset as d

def build_model(
    parser: argparse.ArgumentParser,
    en_vocab: Vocabulary,
    fr_vocab: Vocabulary,
) -> nn.Module:
    # TODO make switch case
    args = parser.parse_args()
    if args.model_type == 'SimpleLSTM':
        SimpleLSTMModel.add_args(parser)
        args = parser.parse_args()
        return SimpleLSTMModel.build_model(
            src_vocab=en_vocab,
            trg_vocab=fr_vocab,
            encoder_embed_dim=args.encoder_embed_dim,
            encoder_hidden_dim=args.encoder_hidden_dim,
            encoder_dropout=args.encoder_dropout,
            encoder_num_layers=args.encoder_layers,
            decoder_embed_dim=args.decoder_embed_dim,
            decoder_hidden_dim=args.decoder_hidden_dim,
            decoder_dropout=args.decoder_dropout,
            decoder_num_layers=args.decoder_layers,
        )
    elif args.model_type == 'AttentionRNN':
        AttentionRNN.add_args(parser)
        args = parser.parse_args()
        return AttentionRNN.build_model(
            src_vocab=en_vocab,
            trg_vocab=fr_vocab,
            encoder_embed_dim=args.encoder_embed_dim,
            encoder_hidden_dim=args.encoder_hidden_dim,
            encoder_dropout=args.encoder_dropout,
            encoder_num_layers=args.encoder_layers,
            decoder_embed_dim=args.decoder_embed_dim,
            decoder_hidden_dim=args.decoder_hidden_dim,
            decoder_dropout=args.decoder_dropout,
            decoder_num_layers=args.decoder_layers,
            teacher_student_ratio=args.teacher_student_ratio,
        )
    else:
        raise Exception(
            "Unknown Model Type: {}".format(args.model_type)
        )

def train(
    train_loader: d.BatchedIterator,
    valid_loader: d.BatchedIterator,
    model: nn.Module,
    epochs: int,
    learning_rate: float,
    weight_decay: float,
    log_dir: str,
    save_dir: str,
    en_vocab: Vocabulary,
    fr_vocab: Vocabulary,
    device: str,
    multi_gpu: bool,
    save_step: int,
    model_name: str,
    optimizer: str,
) -> None:
    model = model.to(device)
    if multi_gpu and device == 'cuda':
       print('Using multi gpu training')
       model = torch.nn.DataParallel(model, device_ids=[0, 1]).cuda()
    
    if optimizer == "sgd":
        print("using stochastic gradient descent optimizer")
        optim = torch.optim.SGD(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    elif optimizer == "adam":
        print("using adam optimizer")
        optim = torch.optim.Adam(model.parameters(), lr=learning_rate, weight_decay=weight_decay)
    else:
        raise Exception("Illegal Optimizer {}".format(optimizer))

    nan_count = 0
    for e in range(epochs):
        total_loss = 0.0
        count = 0
        with tqdm(train_loader, total=len(train_loader)) as pbar:
            for i, data in enumerate(pbar):
                items, _ = data
                (src, src_lengths), (trg, trg_lengths) = items
                # feed everything into model
                # compute loss
                # call backwards
                optim.zero_grad()
                predicted, _ = model.forward(src, src_lengths, trg)
                if not multi_gpu:
                    loss = F.cross_entropy(
                        predicted[:, :-1].contiguous().view(-1, len(fr_vocab)),
                        trg[:, 1:].contiguous().view(-1),
                        ignore_index=fr_vocab.stoi['<pad>'],
                    )
                else:
                    # if using data parallel, loss has to be computed here
                    # there is no longer a model loss function that we have
                    # access to.
                    # TODO: data parallel kills the computer, why?
                    loss = F.cross_entropy(
                        predicted.view(-1, predicted.size(-1)),
                        trg_tensor.view(-1),
                        ignore_index=fr_vocab.word2idx(constants.PAD_TOKEN),
                    )

                if math.isnan(loss.item()):
                    '''
                    Ignore nan loss for backward, and continue forward
                    '''
                    nan_count += 1
                    print('found nan at {}'.format(i))
                    torch.save(
                        model.state_dict(), 
                        os.path.join(save_dir, model_name, 'unk_problem.pt')
                    )
                    return
                loss.backward()
                optim.step()
                total_loss += loss.item()
                count += 1
                pbar.set_postfix(
                    loss_avg=total_loss/(count),
                    epoch="{}/{}".format(e + 1, epochs),
                    curr_loss=loss.item(),
                    nan_count=nan_count,
                )
                pbar.refresh()

                if (i + 1) % save_step == 0:
                    print('Saving model at iteration {} for epoch {}'.format(i, e))
                    model_file_name = "model_epoch_{}_itr_{}".format(e, i)
                    torch.save(
                        model.state_dict(), 
                        os.path.join(save_dir, model_name, model_file_name)
                    )
            print("Summary: Total Loss {} | Count {} | Average {}".format(total_loss, count, total_loss / count))
            model_file_name = "model_epoch_{}_final".format(e)
            print('saving to {}'.format(os.path.join(save_dir, model_name, model_file_name)))
            torch.save(
                model.state_dict(), 
                os.path.join(save_dir, model_name, model_file_name)
            )

def main() -> None:
    parser = get_arg_parser()
    args = parser.parse_args()
    device = "cuda" if torch.cuda.is_available() and args.cuda else "cpu"
    print('using device {}'.format(device))


    print('loading datasets...')
    src = data.Field(include_lengths=True,
               init_token='<sos>', eos_token='<eos>', batch_first=True, fix_length=200)
    trg = data.Field(include_lengths=True,
               init_token='<sos>', eos_token='<eos>', batch_first=True)
    
    if not args.small:
        mt_train = datasets.TranslationDataset(
            path=constants.WMT14_EN_FR_SMALL_TRAIN,
            exts=('.en', '.fr'),
            fields=(src, trg)
        )
    else:
        mt_train, _, _ = datasets.Multi30k.splits(
            exts=('.en', '.de'),
            fields=(src, trg),
        )

    print('loading vocabulary...')
    src.build_vocab(mt_train, min_freq=2, max_size=80000)
    trg.build_vocab(mt_train, max_size=40000)
    print('loaded vocabulary')
    # mt_dev shares the fields, so it shares their vocab objects

    train_loader = data.BucketIterator(
        dataset=mt_train,
        batch_size=args.batch_size,
        sort_key=lambda x: len(x.src), # data.interleave_keys(len(x.src), len(x.trg)),
        sort_within_batch=True,
        device=device
    )

    model = build_model(parser, src.vocab, trg.vocab)

    print('using model...')
    print(model)

    if not os.path.exists(args.log_dir):
        os.makedirs(args.log_dir)
    
    if not os.path.exists(os.path.join(args.save_dir, args.model_name)):
        os.makedirs(os.path.join(args.save_dir, args.model_name))

    train(
        train_loader=train_loader,
        valid_loader=None, # valid_loader,
        model=model,
        epochs=args.num_epochs,
        learning_rate=args.learning_rate,
        weight_decay=args.weight_decay,
        log_dir=args.log_dir,
        save_dir=args.save_dir,
        en_vocab=src.vocab,
        fr_vocab=trg.vocab,
        device=device,
        multi_gpu=args.multi_gpu,
        save_step=args.save_step,
        model_name=args.model_name,
        optimizer=args.optimizer,
    )

if __name__ == "__main__":
    main()