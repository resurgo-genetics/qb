import os
import sys
import time
import pickle
import argparse
import numpy as np
import pandas as pd
from itertools import cycle
from collections import defaultdict
from functools import partial
from multiprocessing import Pool, Manager
from typing import List, Dict, Tuple, Optional

from qanta.preprocess import format_guess
from qanta.guesser.abstract import AbstractGuesser
from qanta.datasets.quiz_bowl import QuestionDatabase
from qanta.util import constants as c
from qanta.buzzer import constants as bc
from qanta.config import conf
from qanta import logging
from qanta.buzzer.util import GUESSERS
from qanta.reporting.report_generator import ReportGenerator
from qanta.util.multiprocess import _multiprocess

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt

log = logging.get(__name__)
N_GUESSERS = len(GUESSERS)
MAXINT = 99999
HISTO_RATIOS = [0, 0.25, 0.5, 0.75, 1.0]

# continuous valued statistics
EOP_STAT_KEYS_0 = [
        'buzz', # did the buzzer buzz
        'choose_best', # did the buzzer choose the best guesser (earliest correct)
        'choose_hopeful', # did the buzzer choose a hopeful guesser
        'rush', # did the buzzer rush (w.r.t to all guessers)
        'late', # did the buzzer buzz too late (w.r.t to all guessers)
        'not_buzzing_when_shouldnt', 
        'reward',
        'hopeful', # is the question hopeful (w.r.t to all guessers)
        'correct' # how many correct buzzers
        ]

# discrete valued statistics
EOP_STAT_KEYS_1 = [
        'choose_guesser', # the guesser chosen by the buzzer
        'best_guesser' # the best guesser
        ]

# overall guesser accuracy and buzzing frequency
HISTO_KEYS_0 = ['acc', 'buzz']  + \
        ['acc_{}'.format(g) for g in GUESSERS] + \
        ['buzz_{}'.format(g) for g in GUESSERS]

HISTO_KEYS_1 = ['correct', # buzzer is correct
         'wrong_hopeful', # buzzer never correct when hopeful
         'wrong_hopeless' # buzzer never correct when hopeless
        ]

LINE_STYLES = {'acc': '-', 'buzz': '-'}
_STYLES = [':', '--', '-.']
for guesser, style in zip(GUESSERS, cycle(_STYLES)):
    LINE_STYLES['acc_{}'.format(guesser)] = style
    LINE_STYLES['buzz_{}'.format(guesser)] = style

def _get_top_guesses(inputs):
    (qnum, question), queue = inputs
    top_guesses = [] # length * n_guessers
    # FIXME because there can be missing guessers, must iterate position first
    for _, position in question.groupby(['sentence', 'token']):
        top_guesses.append([])
        position = position.groupby('guesser')
        for guesser in GUESSERS:
            if guesser not in position.groups:
                top_guesses[-1].append(None)
            else:
                guesses = position.get_group(guesser).sort_values(
                        'score', ascending=False)
                top_guesses[-1].append(guesses.iloc[0].guess)
    if queue is not None:
        queue.put(qnum)
    # transpose top_guesses -> n_guessers * length
    return qnum, list(map(list, zip(*top_guesses)))

def _get_eop_stats(buzzes: Dict[int, List[List[float]]],
                    answers: Dict[int, str], inputs) \
                -> Tuple[int, Dict[str, int]]:
    (qnum, top_guesses), queue = inputs
    buzz = buzzes[qnum]
    answer = answers[qnum]

    # top_guesses: n_guessers * length
    length = len(top_guesses[0])
    if len(buzz) != length:
        raise ValueError("Length of buzzes {0} does not match with \
                guesses {1}".format(len(buzz), length))

    stats = {k: -1 for k in EOP_STAT_KEYS_0 + EOP_STAT_KEYS_1}

    # the first correct position of each guesser
    correct = [g.index(answer) if answer in g else MAXINT for g in top_guesses]
    best_guesser = -1 if np.all(correct == MAXINT) else np.argmin(correct)
    stats['best_guesser'] = best_guesser
    stats['correct'] = sum(x != MAXINT for x in correct)
    stats['hopeful'] = stats['correct'] > 0
    hopeful = stats['hopeful']

    # the buzzing position and chosen guesser
    pos, chosen = -1, -1
    for i in range(length):
        action = np.argmax(buzz[i]) 
        if action < len(GUESSERS):
            pos = i
            chosen = action
            break

    if pos == -1:
        # not buzzing
        stats['buzz'] = 0
        stats['reward'] = 0
        stats['not_buzzing_when_shouldnt'] = int(not hopeful)
    else:
        stats['buzz'] = 1
        stats['choose_guesser'] = chosen
        stats['choose_hopeful'] = int(correct[chosen] != MAXINT)
        stats['reward'] = 10 if pos >= correct[chosen] else -5
        if hopeful:
            stats['choose_best'] = int(chosen == best_guesser)
            stats['late'] = max(0, pos - correct[best_guesser])
            stats['rush'] = max(0, correct[best_guesser] - pos)

    if queue is not None:
        queue.put(qnum)

    return qnum, stats

def _get_his_stats(buzzes: Dict[int, List[List[float]]],
              answers: Dict[int, str], inputs) \
            -> Tuple[int, Dict[str, List[int]]]:
    (qnum, top_guesses), queue = inputs
    buzz = buzzes[qnum]
    answer = answers[qnum]

    # top_guesses: n_guessers * length
    length = len(top_guesses[0])
    if len(buzz) != length:
        raise ValueError("Length of buzzes {0} does not match with \
                guesses {1}".format(len(buzz), length))

    # n_guessers * length -> length * n_guessers
    top_guesses = list(map(list, zip(*top_guesses)))
    guesser_correct = [[int(x == answer) for x in g] for g in top_guesses]

    buzzer_correct = []
    for i, x in enumerate(buzz):
        x = np.argmax(x)
        if x < N_GUESSERS and guesser_correct[i][x]:
            buzzer_correct.append(1)
        else:
            buzzer_correct.append(0)

    stats = {k: [-1 for _ in HISTO_RATIOS] for k in HISTO_KEYS_0 + HISTO_KEYS_1}

    for i, r in enumerate(HISTO_RATIOS):
        pos = int(length * r)
        cor = sum(sum(x) for x in guesser_correct[:pos])
        buz = sum(np.argmax(x) < N_GUESSERS for x in buzz[:pos])
        stats['acc'][i] = int(cor > 0)
        stats['buzz'][i] = int(buz > 0)
        for j, g in enumerate(GUESSERS):
            cor = sum(x[j] for x in guesser_correct[:pos])
            buz = sum(np.argmax(x) == j for x in buzz[:pos])
            stats['acc_{}'.format(g)][i] = int(cor > 0)
            stats['buzz_{}'.format(g)][i] = int(buz > 0)
        buz_cor = sum(buzzer_correct[:pos])
        stats['correct'][i] = int(buz_cor > 0)
        stats['wrong_hopeless'][i] = int(buz_cor == 0 and cor == 0)
        stats['wrong_hopeful'][i] = int(buz_cor == 0 and cor > 0)
        assert stats['correct'][i] + stats['wrong_hopeless'][i] + \
                stats['wrong_hopeful'][i] == 1
        queue.put(qnum)

    return qnum, stats

def get_eop_stats(top_guesses, buzzes, answers, variables, fold, save_dir):
    log.info('[{}] End-of-pipelin reporting'.format(fold))

    inputs = top_guesses.items()
    worker = partial(_get_eop_stats, buzzes, answers)
    eop_stats = _multiprocess(worker, inputs, info='End-of-pipeline stats',
            multi=True)

    # qnum -> key -> int
    eop_stats = {k: v for k, v in eop_stats}
    # key -> int
    _eop_stats = defaultdict(lambda: [])

    eop_output = ""
    for qnum, stat in eop_stats.items():
        for key in EOP_STAT_KEYS_0 + EOP_STAT_KEYS_1:
            if stat[key] != -1:
                _eop_stats[key].append(stat[key])

    for key in EOP_STAT_KEYS_0:
        values = _eop_stats[key]
        value = sum(values) / len(values) if len(values) > 0 else 0
        _eop_stats[key] = value
        output = "{0} {1:.3f}".format(key, value)
        eop_output += output + '\n'
        # print(output)

    for key in EOP_STAT_KEYS_1:
        output = key
        values = _eop_stats[key]
        _eop_stats[key] = dict()
        for i, guesser in enumerate(GUESSERS):
            output += " {0} {1}".format(guesser, values.count(i))
            _eop_stats[key][guesser] = values.count(i)
        eop_output += output + '\n'
        # print(output)

    if variables is not None:
        variables['eop_stats'][fold] = _eop_stats

    return _eop_stats

def get_his_stats(top_guesses, buzzes, answers, variables, fold, save_dir):
    log.info('[{}] Histogram reporting'.format(fold))

    inputs = top_guesses.items()
    worker = partial(_get_his_stats, buzzes, answers)
    his_stats = _multiprocess(worker, inputs, info='Histogram stats',
            multi=True)
    # qnum -> key -> list(int)
    his_stats = {k: v for k, v in his_stats}
    # key -> list(int)
    _his_stats = defaultdict(lambda: [[] for _ in HISTO_RATIOS])

    for stats in his_stats.values():
        for key in HISTO_KEYS_0 + HISTO_KEYS_1:
            for i, r in enumerate(HISTO_RATIOS):
                if stats[key][i] != -1:
                    _his_stats[key][i].append(stats[key][i])

    for key in HISTO_KEYS_0 + HISTO_KEYS_1:
        for i, r in enumerate(HISTO_RATIOS):
            s = _his_stats[key][i]
            _his_stats[key][i] = sum(s) / len(s) if len(s) > 0 else 0

    _his_stats = dict(_his_stats)
    
    his_output = ""
    for i, r in enumerate(HISTO_RATIOS):
        output = "{}:".format(r)
        for key in HISTO_KEYS_0 + HISTO_KEYS_1:
            output += "  {0} {1:.2f}".format(key, _his_stats[key][i])
        his_output += output + '\n'
        # print(output)

    ##### plot lines #####
    his_lines_dir = os.path.join(save_dir, 'his_{}_lines.png'.format(fold))
    lines = []
    for k in HISTO_KEYS_0:
        v = _his_stats[k]
        lines.append(plt.plot(HISTO_RATIOS, v, LINE_STYLES[k], label=k)[0])
    plt.legend(handles=lines)
    plt.savefig(his_lines_dir, dpi=200, format='png')
    plt.clf()

    ##### plot stacked area chart #####
    his_stacked_dir = os.path.join(save_dir, 'his_{}_stacked.png'.format(fold))
    plt.plot([],[],color='c', label='correct')
    plt.plot([],[],color='r', label='wrong_hopeful')
    plt.plot([],[],color='k', label='wrong_hopeless')

    plt.stackplot(list(range(len(HISTO_RATIOS))), 
            _his_stats['correct'], _his_stats['wrong_hopeful'],
            _his_stats['wrong_hopeless'],
            colors=['c','r','k'], alpha=0.5)
    plt.legend()
    plt.savefig(his_stacked_dir, dpi=200, format='png')
    plt.clf()

    if variables is not None:
        variables['his_stats'][fold] = _his_stats
        variables['his_lines'][fold] = his_lines_dir
        variables['his_stacked'][fold] = his_stacked_dir

    return _his_stats

def get_hyper_search(top_guesses, buzzes, answers, variables, fold, save_dir):
    log.info('[{}] Hyperparameter search reporting'.format(fold))

    with open('output/buzzer/cfg_buzzes_{}.pkl'.format(fold), 'rb') as infile:
        cfg_buzzes = pickle.load(infile)
    n_configs = len(cfg_buzzes)
    
    configs, rushs, lates = [], [], []
    for config, buzzes in cfg_buzzes:
        s = get_eop_stats(top_guesses, buzzes, answers, None, fold, save_dir)
        configs.append(config)
        rushs.append(s['rush'])
        lates.append(s['late'])

    config_names = list(range(n_configs))
        
    pos = list(range(n_configs))
    width = 0.25
    fig, ax = plt.subplots(figsize=(10,5))
    plt.bar(pos, rushs, width, alpha=0.5, color='#EE3224')
    plt.bar(pos, lates, width, bottom=rushs, alpha=0.5, color='#F78F1E')

    ax.set_ylabel('%')
    ax.set_title('Test Subject Scores')
    ax.set_xticks([p + 1.42 * width for p in pos])
    ax.set_xticklabels(config_names)

    plt.grid()
    rush_late_dir = os.path.join(save_dir, 'rush_late_{}.png'.format(fold))
    plt.savefig(rush_late_dir, format='png')

    if variables is not None:
        variables['rush_late_plot'][fold] = rush_late_dir
        variables['hype_configs']['dev'] = list(zip(config_names, configs))

def generate(buzzes, answers, guesses_df, variables, fold, save_dir=None,
        multiprocessing=True):

    questions = guesses_df.groupby('qnum')

    # qnum -> n_guessers * length
    top_guesses = _multiprocess(_get_top_guesses, questions, 
        info='Top guesses', multi=True)
    top_guesses = {k: v for k, v in top_guesses}

    inputs = (top_guesses, buzzes, answers, variables, fold, save_dir)

    get_eop_stats(*inputs)
    get_his_stats(*inputs)
    get_hyper_search(*inputs)

def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument('-f', '--fold', default=None)
    return parser.parse_args()

def main(folds, checkpoint_dir=None):
    if checkpoint_dir is not None and os.path.exists(checkpoint_dir):
        with open(checkpoint_dir, 'rb') as infile:
            variables = pickle.load(infile)
        report(variables, save_dir)
        return
    
    all_questions = QuestionDatabase().all_questions()
    answers = {k: format_guess(v.page) for k, v in all_questions.items()}

    save_dir = 'output/summary/new_performance/'
    if not os.path.exists(save_dir):
        os.makedirs(save_dir)

    # feature -> fold -> value
    variables = defaultdict(lambda: defaultdict())
    for fold in folds:
        guesses_df = AbstractGuesser.load_guesses(
                bc.GUESSES_DIR, folds=[fold])

        buzzes_dir = bc.BUZZES_DIR.format(fold)
        with open(buzzes_dir, 'rb') as infile:
            buzzes = pickle.load(infile)
        log.info('Buzzes loaded from {}.'.format(buzzes_dir))

        generate(buzzes, answers, guesses_df, variables, fold, save_dir)

    for key, value in variables.items():
        variables[key] = dict(value)
    variables = dict(variables)

    # checkpoint_dir = os.path.join(save_dir, 'checkpoint.pkl')
    # with open(checkpoint_dir, 'wb') as outfile:
    #     pickle.dump(variables, outfile)

    report(variables, save_dir)

def report(variables, save_dir):
    # use this to have jinja skip non-existent features
    jinja_keys = ['his_lines', 'his_stacked', 'rush_late_plot', 'hype_configs']
    _variables = {k: dict() for k in jinja_keys}
    _variables.update(variables)
    output = os.path.join(save_dir, 'new_performance.pdf')
    report_generator = ReportGenerator('new_performance.md')
    report_generator.create(_variables, output)

if __name__ == '__main__':
    args = parse_args()
    if args.fold != None:
        folds = [args.fold]
    else:
        folds = c.BUZZ_FOLDS
    main(folds)