from data_reader import DataSet
import numpy as np
import os
import pickle
import logging
import torch
import gc
import argparse
from models.DocumentClassificationModel import DocumentClassificationModel
from dependency_decoding import chu_liu_edmonds
import tqdm
import torch.optim as optim
import torch
import traceback
import torch.nn as nn
import torch.nn.functional as F


def load_data(config):
    train, dev, test, embeddings, vocab = pickle.load(open(config.data_file, 'rb'))
    trainset, devset, testset = DataSet(train), DataSet(dev), DataSet(test)
    vocab = dict([(v['index'],k) for k,v in vocab.items()])
    trainset.sort(reverse=False)
    train_batches = trainset.get_batches(config.batch_size, config.epochs, rand=False)
    dev_batches = devset.get_batches(config.batch_size, 1, rand=False)
    test_batches = testset.get_batches(config.batch_size, 1, rand=False)
    temp_train = trainset.get_batches(config.batch_size, config.epochs, rand=True)
    dev_batches = [i for i in dev_batches]
    test_batches = [i for i in test_batches]
    temp_train = [i for i in temp_train]
    return len(train), train_batches, dev_batches, test_batches, embeddings, vocab, temp_train


def get_feed_dict(batch, device):
    batch_size = len(batch)
    doc_l_matrix = np.ones([batch_size], np.int32)
    for i, instance in enumerate(batch):
        n_sents = len(instance.token_idxs)
        doc_l_matrix[i] = n_sents if n_sents>0 else 1
    max_doc_l = np.max(doc_l_matrix)
    max_sent_l = max([max([len(sent) for sent in doc.token_idxs]) for doc in batch])
    token_idxs_matrix = np.zeros([batch_size, max_doc_l, max_sent_l], np.int32)
    sent_l_matrix = np.ones([batch_size, max_doc_l], np.int32)
    gold_matrix = np.zeros([batch_size], np.int32)
    mask_tokens_matrix = np.ones([batch_size, max_doc_l, max_sent_l], np.float32)
    mask_sents_matrix = np.ones([batch_size, max_doc_l], np.float32)
    for i, instance in enumerate(batch):
        n_sents = len(instance.token_idxs)
        gold_matrix[i] = instance.goldLabel
        for j, sent in enumerate(instance.token_idxs):
            token_idxs_matrix[i, j, :len(sent)] = np.asarray(sent)
            mask_tokens_matrix[i, j, len(sent):] = 0
            sent_l_matrix[i, j] = len(sent) if len(sent)>0 else 1
        mask_sents_matrix[i, n_sents:] = 0
    mask_parser_1 = np.ones([batch_size, max_doc_l, max_doc_l], np.float32)
    mask_parser_2 = np.ones([batch_size, max_doc_l, max_doc_l], np.float32)
    mask_parser_1[:, :, 0] = 0
    mask_parser_2[:, 0, :] = 0
    if max_doc_l == 1 or max_sent_l == 1 or max_doc_l >30 or max_sent_l>30:
        return False, {}
    try:
        feed_dict = {'token_idxs': torch.LongTensor(token_idxs_matrix).to(device),
                     'gold_labels': torch.LongTensor(gold_matrix).to(device),
                     'mask_tokens': torch.FloatTensor(mask_tokens_matrix).to(device),
                     'mask_sents': torch.FloatTensor(mask_sents_matrix).to(device),
                     'sent_l': sent_l_matrix,
                     'doc_l': doc_l_matrix}
    except:
        return False, [batch_size * max_doc_l * max_sent_l * max_sent_l / (16 * 200000) + 1]
    return True, feed_dict


def evaluate(model, test_batches, device, criterion):
    corr_count, all_count = 0, 0
    model.eval()
    count = 0
    total_loss = 0
    for ct, batch in test_batches:
        #print("Batch : "+str(count))
        value, feed_dict = get_feed_dict(batch, device) # batch = [Instances], feed_dict = {inputs}
        if not value:
            continue
        output, sent_attention_matrix, doc_attention_matrix = model.forward(feed_dict)
        total_loss = criterion(output, feed_dict['gold_labels']).item()
        predictions = output.max(1)[1]
        corr_count += torch.sum(predictions == feed_dict['gold_labels']).item()
        all_count += len(batch)
        count += 1
        del feed_dict['token_idxs']
        del feed_dict['gold_labels']
        del feed_dict
        torch.cuda.empty_cache()
    print(corr_count, all_count)
    #print("Test Loss: "+str(total_loss/count))
    acc_test = 1.0 * corr_count / all_count
    return acc_test

def extract_structures(model, test_batches, device, vocab, dirName):
    model.eval()
    dirName = dirName+"/structures"
    if not os.path.exists(dirName):
        os.mkdir(dirName)
        print("Directory " , dirName ,  " Created ")
    count=0
    for ct, batch in test_batches:
        value, feed_dict = get_feed_dict(batch, device)
        if not value:
            continue
        output, sent_attention_matrix, doc_attention_matrix = model.forward(feed_dict)
        batch_size = doc_attention_matrix.size(0)
        sent_size = doc_attention_matrix.size(1)
        token_size = sent_attention_matrix.size(1)

        for i in range(len(batch)):
            fileName = dirName+"/"+str(count)+".txt"
            count += 1
            fp = open(fileName, "w")
            #print("\nDoc: "+str(count)+"\n")
            fp.write("Doc: "+str(count)+"\n")

            l = len(batch[i].token_idxs)
            sent_no = 0
            for sent in batch[i].token_idxs:
                printstr = ''
                #scores = str_scores_sent[sent_no][0:l, 0:l]
                token_count = 0
                for token in sent:
                    printstr += vocab[token]+" "
                    token_count = token_count + 1
                #print(printstr)
                fp.write(printstr+"\n")

                scores = sent_attention_matrix[sent_no][0:token_count, 0:token_count]
                shape2 = sent_attention_matrix[sent_no][0:token_count,0:token_count].size()
                row = torch.ones([1, shape2[1]+1]).to(device)
                column = torch.zeros([shape2[0], 1]).to(device)
                new_scores = torch.cat([column, scores], dim=1)
                new_scores = torch.cat([row, new_scores], dim=0)
                heads, tree_score = chu_liu_edmonds(new_scores.data.cpu().numpy().astype(np.float64))
                #print(heads, tree_score)
                fp.write(str(heads)+" ")
                fp.write(str(tree_score)+"\n")

            #doc_attention_matrix = doc_attention_matrix[:,:,1:]

            sentence_importance_vector = doc_attention_matrix[:,:,1:].sum(dim=1) * feed_dict['mask_sents']
            sentence_importance_vector = sentence_importance_vector / sentence_importance_vector.sum(dim=1, keepdim=True).repeat(1, sentence_importance_vector.size(1))
            token_level_sentence_scores = sentence_importance_vector.unsqueeze(1).repeat(1, token_size, 1).view(batch_size, sent_size*token_size)
            #doc_attention_matrix = doc_attention_matrix[:,:,1:]
            #print(token_level_sentence_scores)
            shape2 = doc_attention_matrix[i][0:l,0:l].size()
            row = torch.ones([1, shape2[1]+1]).to(device)
            column = torch.zeros([shape2[0], 1]).to(device)
            scores = doc_attention_matrix[i][0:l, 0:l]
            new_scores = torch.cat([column, scores], dim=1)
            new_scores = torch.cat([row, new_scores], dim=0)
            heads, tree_score = chu_liu_edmonds(new_scores.data.cpu().numpy().astype(np.float64))
            #print(heads, tree_score)
            fp.write("\n")
            fp.write(str(heads)+" ")
            fp.write(str(tree_score)+"\n")
            fp.close()



def run(config, device, reload_path):

    num_examples, train_batches, dev_batches, test_batches, embedding_matrix, vocab, temp_train = load_data(config)
    config.n_embed, config.d_embed = embedding_matrix.shape

    config.dim_hidden = config.dim_sem+config.dim_str

    print(config)

    # model = DocumentClassificationModel(device, config.n_embed, config.d_embed, config.dim_hidden, config.dim_hidden, 1, 1, config.dim_sem, pretrained=embedding_matrix, dropout=config.dropout, bidirectional=True, py_version=config.pytorch_version).to(device)
    criterion = nn.CrossEntropyLoss()

    with open(reload_path, 'rb') as f:
        model = torch.load(f)
    # after load the rnn params are not a continuous chunk of memory
    # this makes them a continuous chunk, and will speed up forward pass
    model.sentence_encoder.bilstm.flatten_parameters()
    model.document_encoder.bilstm.flatten_parameters()
    acc_test = evaluate(model, test_batches, device, criterion)
    print('Test ACC: {}\n'.format(acc_test))
    dirName = "structures"
    extract_structures(model, test_batches, device, vocab, dirName)




parser = argparse.ArgumentParser(description='PyTorch Structured Attention Model')
parser.add_argument('--cuda', action='store_true', default=False, help='use CUDA')
parser.add_argument('--seed', type=int, default=1,help='random seed')
parser.add_argument('--batch_size', type=int, default=8,help='batchsize')
parser.add_argument('--lr', type=float, default=0.05,help='learning rate')
parser.add_argument('--pytorch_version', type=str, default='nightly',help='location of the data corpus')
parser.add_argument('--data_file', type=str, default='data/yelp-2013/yelp-2013-all.pkl',help='location of the data corpus')
parser.add_argument('--reload_path', type=str, default='./saved_models/best_model.pth',help='location of the best model and generated files to save')
parser.add_argument('--word_emsize', type=int, default=300,help='size of word embeddings')

parser.add_argument('--dim_str', type=int, default=50,help='size of word embeddings')
parser.add_argument('--dim_sem', type=int, default=50,help='size of word embeddings')
parser.add_argument('--dim_output', type=int, default=5,help='size of word embeddings')
parser.add_argument('--n_embed', type=int, default=49030,help='size of word embeddings')
parser.add_argument('--d_embed', type=int, default=200,help='size of word embeddings')

parser.add_argument('--nlayers', type=int, default=1,help='number of layers')
parser.add_argument('--dropout', type=float, default=0.2,help='dropout applied to layers (0 = no dropout)')
parser.add_argument('--clip', type=float, default=5,help='gradient clip')
parser.add_argument('--log_period', type=float, default=100,help='log interval')
parser.add_argument('--epochs', type=int, default=50,help='epochs')

args = parser.parse_args()
cuda = args.cuda
total_epochs = args.epochs
dropout = args.dropout
seed = args.seed
num_layers = args.nlayers
word_emb_size = args.word_emsize
data_path = args.data_file
reload_path = args.reload_path
lr = args.lr
clip = args.clip
log_period = args.log_period


torch.manual_seed(seed)
if torch.cuda.is_available():
    if not cuda:
        print("WARNING: You have a CUDA device, so you should probably run with --cuda")

device = torch.device("cuda" if args.cuda else "cpu")

run(args, device, reload_path)
