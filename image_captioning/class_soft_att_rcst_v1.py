#! encoding: UTF-8

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import ipdb
import numpy as np

import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import *

import opts
from classLSTMSoftAttCore import LSTMSoftAttentionCore
from classLSTMCore import LSTMCore


def to_contiguous(tensor):
    if tensor.is_contiguous():
        return tensor
    else:
        return tensor.contiguous()


class ShowAttendTellModel(nn.Module):
    def __init__(self, opt):
        super(ShowAttendTellModel, self).__init__()

        self.vocab_size = opt.vocab_size
        self.input_encoding_size = opt.input_encoding_size
        self.lstm_size = opt.lstm_size
        self.drop_prob_lm = opt.drop_prob_lm
        self.seq_length = opt.seq_length

        self.fc_feat_size = opt.fc_feat_size
        self.conv_feat_size = opt.conv_feat_size
        self.conv_att_size = opt.conv_att_size
        self.att_hidden_size = opt.att_hidden_size

        self.use_cuda = opt.use_cuda

        # Schedule sampling probability
        self.ss_prob = 0.0

        # 由 fc feature 初始化 LSTM 的 state, (c, h)
        self.fc2h = nn.Linear(self.fc_feat_size, self.lstm_size)

        self.core = LSTMSoftAttentionCore(self.input_encoding_size,
                                          self.lstm_size,
                                          self.conv_feat_size,
                                          self.conv_att_size,
                                          self.att_hidden_size,
                                          self.drop_prob_lm)

        self.embed = nn.Embedding(self.vocab_size + 1, self.input_encoding_size)
        self.logit = nn.Linear(self.lstm_size, self.vocab_size)

        self.init_weights()

        # 接着 soft attention 添加 reconstruct 部分
        self.rcst_scale = opt.rcst_weight
        self.rcst_lstm = LSTMCore(self.input_encoding_size, self.lstm_size, self.drop_prob_lm)
        self.hidden_state_2_pre_hidden_state_context = nn.Linear(self.lstm_size,
                                                                 self.input_encoding_size + self.conv_feat_size)

        self.rcst_init_weights()

    def init_weights(self):
        initrange = 0.1
        self.embed.weight.data.uniform_(-initrange, initrange)
        self.fc2h.weight.data.uniform_(-initrange, initrange)
        self.logit.weight.data.uniform_(-initrange, initrange)
        self.logit.bias.data.fill_(0)

    # 初始化新添加的 reconstruct 部分
    def rcst_init_weights(self):
        initrange = 0.1
        self.hidden_state_2_pre_hidden_state_context.weight.data.uniform_(-initrange, initrange)
        self.hidden_state_2_pre_hidden_state_context.bias.data.fill_(0)

    def forward(self, fc_feats, att_feats, seq):
        batch_size = fc_feats.size(0)

        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)
        outputs = []

        for i in range(seq.size(1)):
            if i >= 1 and self.ss_prob > 0.0:  # otherwiste no need to sample
                sample_prob = fc_feats.data.new(batch_size).uniform_(0, 1)
                sample_mask = sample_prob < self.ss_prob
                if sample_mask.sum() == 0:
                    it = seq[:, i].clone()
                else:
                    sample_ind = sample_mask.nonzero().view(-1)
                    it = seq[:, i].data.clone()
                    prob_prev = torch.exp(outputs[-1].data)  # fetch prev distribution: shape Nx(M+1)
                    it.index_copy_(0, sample_ind, torch.multinomial(prob_prev, 1).view(-1).index_select(0, sample_ind))
                    it = Variable(it, requires_grad=False)
            else:
                it = seq[:, i].clone()

            # break if all the sequences end
            if i >= 1 and seq[:, i].data.sum() == 0:
                break

            xt = self.embed(it)

            output, state = self.core.forward(xt, att_feats, state)
            output = F.log_softmax(self.logit(output.squeeze(0)))
            outputs.append(output)

        return torch.cat([_.unsqueeze(1) for _ in outputs], 1).contiguous()  # batch * 19 * vocab_size

    # reconstruct 部分
    def rcst_forward(self, fc_feats, att_feats, seq, mask):
        batch_size = fc_feats.size(0)

        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)

        rcst_init_h = init_h.clone()
        rcst_init_c = init_c.clone()
        rcst_state = (rcst_init_h, rcst_init_c)

        previous_hidden_state = init_h.clone()

        outputs = []
        rcst_loss = 0.0

        for i in range(seq.size(1)-1):
            if i >= 1 and self.ss_prob > 0.0:  # otherwise no need to sample
                sample_prob = fc_feats.data.new(batch_size).uniform_(0, 1)
                sample_mask = sample_prob < self.ss_prob
                if sample_mask.sum() == 0:
                    it = seq[:, i].clone()
                else:
                    sample_ind = sample_mask.nonzero().view(-1)
                    it = seq[:, i].data.clone()
                    prob_prev = torch.exp(outputs[-1].data)  # fetch prev distribution: shape Nx(M+1)
                    it.index_copy_(0, sample_ind, torch.multinomial(prob_prev, 1).view(-1).index_select(0, sample_ind))
                    it = Variable(it, requires_grad=False)
            else:
                it = seq[:, i].clone()

            # break if all the sequences end
            if i >= 1 and seq[:, i].data.sum() == 0:
                break

            xt = self.embed(it)

            output, state, context_weighted = self.core.rcst_forward(xt, att_feats, state)
            logit_words = F.log_softmax(self.logit(output.squeeze(0)))
            outputs.append(logit_words)

            # rcst 部分
            rcst_output, rcst_state = self.rcst_lstm.forward(output, rcst_state)

            rcst_hidden_state = self.hidden_state_2_pre_hidden_state_context(rcst_output)

            # 将 previous hidden state(batch * 512) 与 context(batch * 1536) 连接起来, 这是 reconstruct 的目标
            pre_h_plus_ctx = torch.cat((previous_hidden_state.squeeze(dim=0), context_weighted), dim=1)
            rcst_diff = rcst_hidden_state - pre_h_plus_ctx
            rcst_mask = mask[:, i].contiguous().view(-1, batch_size).repeat(1, self.lstm_size + self.conv_feat_size)
            rcst_loss += torch.sum(torch.sum(torch.mul(rcst_diff, rcst_diff) * rcst_mask, dim=1)) / batch_size * self.rcst_scale

            # previous hidden state
            previous_hidden_state = state[0].clone()

        return torch.cat([_.unsqueeze(1) for _ in outputs], 1).contiguous(), rcst_loss

    def get_init_state(self, fc_feats):
        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)
        return state

    def one_time_step(self, xt, att_feats, state):
        # xt = self.embed(Variable(it, requires_grad=False))
        output, state = self.core.forward(xt, att_feats, state)

        # logprobs = F.log_softmax(self.logit(output))
        logit = self.logit(output)

        return logit, state

    def sample_beam(self, fc_feats, att_feats, init_index, opt={}):
        beam_size = opt.get('beam_size', 10)  # 如果不能取到 beam_size 这个变量, 则令 beam_size 为 10
        batch_size = fc_feats.size(0)
        fc_feat_size = fc_feats.size(1)

        seq = torch.LongTensor(self.seq_length, batch_size).zero_()
        seqLogprobs = torch.FloatTensor(self.seq_length, batch_size)

        top_seq = []
        top_prob = [[] for _ in range(batch_size)]

        self.done_beams = [[] for _ in range(batch_size)]

        for k in range(batch_size):
            init_h = self.fc2h(fc_feats[k].unsqueeze(0).expand(beam_size, fc_feat_size))
            init_h = init_h.unsqueeze(0)
            init_c = init_h.clone()
            state = (init_h, init_c)

            att_feats_current = att_feats[k].unsqueeze(0).expand(beam_size, att_feats.size(1), att_feats.size(2))
            att_feats_current = att_feats_current.contiguous()

            beam_seq = torch.LongTensor(self.seq_length, beam_size).zero_()
            beam_seq_logprobs = torch.FloatTensor(self.seq_length, beam_size).zero_()
            beam_logprobs_sum = torch.zeros(beam_size)  # running sum of logprobs for each beam
            for t in range(self.seq_length + 1):
                if t == 0:  # input BOS
                    it = fc_feats.data.new(beam_size).long().fill_(init_index)
                    xt = self.embed(Variable(it, requires_grad=False))
                    # xt = self.img_embed(fc_feats[k:k+1]).expand(beam_size, self.input_encoding_size)
                else:
                    """
                    perform a beam merge.
                    that is, for every previous beam we now many new possibilities to branch out
                    we need to resort our beams to maintain the loop invariant of keeping
                    the top beam_size most likely sequences.
                    """
                    logprobsf = logprobs.float()  # lets go to CPU for more efficiency in indexing operations
                    # ys: beam_size * (Vab_size + 1)
                    ys, ix = torch.sort(logprobsf, 1, True)  # sorted array of logprobs along each previous beam (last true = descending)
                    candidates = []
                    cols = min(beam_size, ys.size(1))
                    rows = beam_size
                    if t == 1:  # at first time step only the first beam is active
                        rows = 1
                    for c in range(cols):
                        for q in range(rows):
                            # compute logprob of expanding beam q with word in (sorted) position c
                            local_logprob = ys[q, c]
                            candidate_logprob = beam_logprobs_sum[q] + local_logprob
                            if t > 1 and beam_seq[t - 2, q] == 0:
                                continue
                            candidates.append({'c': ix.data[q, c], 'q': q, 'p': candidate_logprob.data[0], 'r': local_logprob.data[0]})

                    if len(candidates) == 0:
                        break

                    candidates = sorted(candidates, key=lambda x: -x['p'])

                    # construct new beams
                    new_state = [_.clone() for _ in state]
                    if t > 1:
                        # well need these as reference when we fork beams around
                        beam_seq_prev = beam_seq[:t - 1].clone()
                        beam_seq_logprobs_prev = beam_seq_logprobs[:t - 1].clone()

                    for vix in range(min(beam_size, len(candidates))):
                        v = candidates[vix]
                        # fork beam index q into index vix
                        if t > 1:
                            beam_seq[:t - 1, vix] = beam_seq_prev[:, v['q']]
                            beam_seq_logprobs[:t - 1, vix] = beam_seq_logprobs_prev[:, v['q']]

                        # rearrange recurrent states
                        for state_ix in range(len(new_state)):
                            # copy over state in previous beam q to new beam at vix
                            new_state[state_ix][0, vix] = state[state_ix][0, v['q']]  # dimension one is time step

                        # append new end terminal at the end of this beam
                        beam_seq[t - 1, vix] = v['c']  # c'th word is the continuation
                        beam_seq_logprobs[t - 1, vix] = v['r']  # the raw logprob here
                        beam_logprobs_sum[vix] = v['p']  # the new (sum) logprob along this beam

                        if v['c'] == 0 or t == self.seq_length:
                            # END token special case here, or we reached the end.
                            # add the beam to a set of done beams
                            self.done_beams[k].append({'seq': beam_seq[:, vix].clone(), 'logps': beam_seq_logprobs[:, vix].clone(), 'p': beam_logprobs_sum[vix]})

                    # encode as vectors
                    it = beam_seq[t - 1]
                    if self.use_cuda:
                        xt = self.embed(Variable(it.cuda()))
                    else:
                        xt = self.embed(Variable(it))

                if t >= 1:
                    state = new_state

                output, state = self.core.forward(xt, att_feats_current, state)
                logprobs = F.log_softmax(self.logit(output))

            self.done_beams[k] = sorted(self.done_beams[k], key=lambda x: -x['p'])
            seq[:, k] = self.done_beams[k][0]['seq']  # the first beam has highest cumulative score
            seqLogprobs[:, k] = self.done_beams[k][0]['logps']

            # save result
            l = len(self.done_beams[k])
            top_seq_cur = torch.LongTensor(l, self.seq_length).zero_()

            for temp_index in range(l):
                top_seq_cur[temp_index] = self.done_beams[k][temp_index]['seq'].clone()
                top_prob[k].append(self.done_beams[k][temp_index]['p'])

            top_seq.append(top_seq_cur)

        # return the samples and their log likelihoods
        return seq.transpose(0, 1), seqLogprobs.transpose(0, 1), top_seq, top_prob

    def sample(self, fc_feats, att_feats, init_index, opt={}):
        sample_max = opt.get('sample_max', 1)
        beam_size = opt.get('beam_size', 1)
        temperature = opt.get('temperature', 1.0)

        if beam_size > 1:
            return self.sample_beam(fc_feats, att_feats, init_index, opt)

        batch_size = fc_feats.size(0)
        seq = []
        seqLogprobs = []
        logprobs_all = []

        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)

        for t in range(self.seq_length):
            if t == 0:
                it = fc_feats.data.new(batch_size).long().fill_(init_index)
            elif sample_max:
                sampleLogprobs, it = torch.max(logprobs.data, 1)
                it = it.view(-1).long()
            else:
                if temperature == 1.0:
                    prob_prev = torch.exp(logprobs.data).cpu()  # fetch prev distribution: shape Nx(M+1)
                else:
                    # scale logprobs by temperature
                    prob_prev = torch.exp(torch.div(logprobs.data, temperature)).cpu()

                if self.use_cuda:
                    it = torch.multinomial(prob_prev, 1).cuda()
                else:
                    it = torch.multinomial(prob_prev, 1)

                sampleLogprobs = logprobs.gather(1, Variable(it, requires_grad=False))  # gather the logprobs at sampled positions
                it = it.view(-1).long()  # and flatten indices for downstream processing

            xt = self.embed(Variable(it, requires_grad=False))

            if t >= 1:
                if t == 1:
                    unfinished = it > 0
                else:
                    unfinished *= (it > 0)
                if unfinished.sum() == 0:
                    break
                it = it * unfinished.type_as(it)
                seq.append(it)
                seqLogprobs.append(sampleLogprobs.view(-1))

            output, state = self.core.forward(xt, att_feats, state)

            logprobs = F.log_softmax(self.logit(output))
            logprobs_all.append(logprobs)

        greedy_seq = torch.cat([_.unsqueeze(1) for _ in seq], 1)
        greedy_seqLogprobs = torch.cat([_.unsqueeze(1) for _ in seqLogprobs], 1)
        greedy_logprobs_all = torch.cat([_.unsqueeze(1) for _ in logprobs_all], 1).contiguous()

        return greedy_seq, greedy_seqLogprobs, greedy_logprobs_all

    def teacher_forcing_get_hidden_states(self, fc_feats, att_feats, seq):
        batch_size = fc_feats.size(0)

        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)
        outputs = []

        for i in range(seq.size(1)):
            if i >= 1 and self.ss_prob > 0.0:  # otherwiste no need to sample
                sample_prob = fc_feats.data.new(batch_size).uniform_(0, 1)
                sample_mask = sample_prob < self.ss_prob
                if sample_mask.sum() == 0:
                    it = seq[:, i].clone()
                else:
                    sample_ind = sample_mask.nonzero().view(-1)
                    it = seq[:, i].data.clone()
                    prob_prev = torch.exp(outputs[-1].data)  # fetch prev distribution: shape Nx(M+1)
                    it.index_copy_(0, sample_ind, torch.multinomial(prob_prev, 1).view(-1).index_select(0, sample_ind))
                    it = Variable(it, requires_grad=False)
            else:
                it = seq[:, i].clone()

            # break if all the sequences end
            if i >= 1 and seq[:, i].data.sum() == 0:
                break

            xt = self.embed(it)

            output, state = self.core.forward(xt, att_feats, state)
            output = F.log_softmax(self.logit(output.squeeze(0)))
            outputs.append(output)

        # 返回 hidden states
        return state[0]

    def free_running_get_hidden_states(self, fc_feats, att_feats, init_index):
        batch_size = fc_feats.size(0)
        seq = []
        seqLogprobs = []
        logprobs_all = []

        init_h = self.fc2h(fc_feats)
        init_h = init_h.unsqueeze(0)
        init_c = init_h.clone()
        state = (init_h, init_c)

        for t in range(self.seq_length):
            if t == 0:  # input BOS
                it = fc_feats.data.new(batch_size).long().fill_(init_index)
            else:
                sampleLogprobs, it = torch.max(logprobs.data, 1)
                it = it.view(-1).long()

            xt = self.embed(Variable(it, requires_grad=False))

            if t >= 1:
                # stop when all finished
                if t == 1:
                    unfinished = it > 0
                else:
                    unfinished *= (it > 0)
                if unfinished.sum() == 0:
                    break
                it = it * unfinished.type_as(it)
                seq.append(it)  # seq[t] the input of t+2 time step
                seqLogprobs.append(sampleLogprobs.view(-1))

            output, state = self.core.forward(xt, att_feats, state)
            logprobs = F.log_softmax(self.logit(output))
            logprobs_all.append(logprobs)

        return state[0]
