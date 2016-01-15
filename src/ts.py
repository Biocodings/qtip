#!/usr/bin/env python

from __future__ import print_function
from subprocess import Popen, PIPE

"""
ts.py

A "tandem simulator," which wraps an alignment tool as it runs, eavesdrops on
input and output, simulates a new dataset similar to the input data, aligns
it, uses those alignments as training data to build a model to predict MAPQ,
then re-calcualtes MAPQs for the original input using that predictor.
"""

import os
from os.path import join, getsize
import sys
import time
import logging
import errno
import resource
import numpy as np
import random
import shutil
try:
    from Queue import Queue, Empty, Full
except ImportError:
    from queue import Queue, Empty, Full  # python 3.x
from operator import itemgetter

# Modules that are part of the tandem simulator
from bowtie2 import Bowtie2
from bwamem import BwaMem
from snap import SnapAligner
from tempman import TemporaryFileManager
from model_fam import model_family
from feature_table import FeatureTableReader
from fit import MapqFit

__author__ = "Ben Langmead"
__email__ = "langmea@cs.jhu.edu"

bin_dir = os.path.dirname(os.path.realpath(__file__))

VERSION = '0.2.1'


class Timing(object):

    def __init__(self):
        self.labs = []
        self.timers = dict()

    def start_timer(self, lab):
        self.labs.append(lab)
        self.timers[lab] = time.time()

    def end_timer(self, lab):
        self.timers[lab] = time.time() - self.timers[lab]

    def __str__(self):
        ret = []
        for lab in self.labs:
            ret.append('\t'.join([lab, str(self.timers[lab])]))
        return '\n'.join(ret) + '\n'


def sanity_check_binary(exe):
    if not os.path.exists(exe):
        raise RuntimeError('Binary "%s" does not exist' % exe)
    else:
        if not os.access(exe, os.X_OK):
            raise RuntimeError('Binary "%s" exists but is not executable' % exe)


def mkdir_quiet(dr):
    # Create output directory if needed
    if not os.path.isdir(dr):
        try:
            os.makedirs(dr)
        except OSError as exception:
            if exception.errno != errno.EEXIST:
                raise


def recursive_size(dr):
    tot = 0
    for root, dirs, files in os.walk(dr):
        tot += sum(getsize(join(root, name)) for name in files)
    return tot


def seed_all(seed):
    random.seed(seed)
    np.random.seed(seed)


def _nop():
    pass


def _at_least_one_read_aligned(sam_fn):
    with open(sam_fn) as fh:
        for ln in fh:
            if ln[0] != '@':
                return True
    return False


def _cat(fns, dst_fn):
    with open(dst_fn, 'wb') as ofh:
        for fn in fns:
            with open(fn, 'rb') as fh:
                shutil.copyfileobj(fh, ofh)


def go(args, aligner_args, aligner_unpaired_args, aligner_paired_args):

    tim = Timing()
    tim.start_timer('Overall')

    if args['vanilla_output'] is not None and args['output_directory'] is not None:
        logging.warning("--vanilla-output overrides and disables --output-directory")
        args['output_directory'] = None

    if args['vanilla_output'] is None and args['output_directory'] is None:
        args['output_directory'] = 'out'

    if args['vanilla_output'] is not None and args['keep_intermediates']:
        logging.warning("--vanilla-output overrides and disables --keep-intermediates")
        args['keep_intermediates'] = False

    # Create output directory if needed
    odir = None
    if args['output_directory'] is not None:
        odir = args['output_directory']
        mkdir_quiet(odir)

    # Set up logger
    format_str = '%(asctime)s:%(levelname)s:%(message)s'
    level = logging.DEBUG if args['verbose'] else logging.INFO
    logging.basicConfig(format=format_str, datefmt='%m/%d/%y-%H:%M:%S', level=level)
    if args['output_directory'] is not None:
        fn = join(odir, 'ts_logs.txt')
        fh = logging.FileHandler(fn)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter(format_str))
        logging.getLogger('').addHandler(fh)

    if args['U'] is not None and args['m1'] is not None:
        raise RuntimeError('Input must consist of only unpaired or only paired-end reads')
    
    # Start building alignment command; right now we support Bowtie 2, BWA-MEM and SNAP
    aligner_class = Bowtie2
    align_cmd = None
    if args['aligner'] == 'bowtie2':
        align_cmd = 'bowtie2 '
        if args['bt2_exe'] is not None:
            align_cmd = args['bt2_exe'] + " "
        aligner_args.extend(['--mapq-extra'])
    elif args['aligner'] == 'bwa-mem':
        align_cmd = 'bwa mem '
        if args['bwa_exe'] is not None:
            align_cmd = args['bwa_exe'] + ' mem '
        aligner_class = BwaMem
    elif args['aligner'] == 'snap':
        align_cmd = 'snap-aligner '
        if args['snap_exe'] is not None:
            align_cmd = args['snap_exe'] + ' '
        aligner_class = SnapAligner
        aligner_args.extend(['-='])
    elif args['aligner'] is not None:
        raise RuntimeError('Aligner not supported: "%s"' % args['aligner'])

    # for storing temp files and keep track of how big they get
    temp_man = TemporaryFileManager(args['temp_directory'])

    def _get_pass1_file_prefix():
        """
        Return the file prefix that should be used for naming intermediate
        files generated by qsim-parse when parsing input SAM.
        """
        if args['keep_intermediates']:
            return join(odir, 'input_intermediates'), _nop
        else:
            dr = temp_man.get_dir('input_intermediates')
            assert os.path.isdir(dr)

            def _purge():
                temp_man.remove_group('input_intermediates')
            return join(dr, 'tmp'), _purge

    def _get_pass2_file_prefix():
        """
        Return the file prefix that should be used for naming intermediate
        files generated by qsim-parse when parsing the tandem SAM.
        """
        if args['keep_intermediates']:
            return join(odir, 'tandem_intermediates'), _nop
        else:
            dr = temp_man.get_dir('tandem_intermediates')
            assert os.path.isdir(dr)

            def _purge():
                temp_man.remove_group('tandem_intermediates')
            return join(dr, 'tmp'), _purge

    def _get_pass3_file_prefix():
        """
        Return the file prefix that should be used for naming intermediate
        files generated by qsim-parse when parsing the tandem SAM.
        """
        if args['keep_intermediates']:
            return join(odir, 'rewrite_intermediates'), _nop
        else:
            dr = temp_man.get_dir('rewrite_intermediates')
            assert os.path.isdir(dr)

            def _purge():
                temp_man.remove_group('rewrite_intermediates')
            return join(dr, 'tmp'), _purge

    parse_input_exe = "%s/qsim-parse" % bin_dir
    rewrite_exe = "%s/qsim-rewrite" % bin_dir

    def _get_input_sam_fn():
        if args['keep_intermediates']:
            return join(odir, 'input.sam'), _nop
        else:
            dr = temp_man.get_dir('input_alignments')

            def _purge():
                temp_man.remove_group('input_alignments')
            return join(dr, 'tmp'), _purge

    def _get_predictions_fn():
        if args['keep_intermediates']:
            return join(odir, 'predictions.csv'), _nop
        else:
            dr = temp_man.get_dir('predictions')

            def _purge():
                temp_man.remove_group('predictions')
            return join(dr, 'predictions.csv'), _purge

    def _get_final_prefix():
        if args['vanilla_output'] is not None:
            _ret = args['vanilla_output']
            if _ret.endswith('.sam'):
                _ret = _ret[:-4]
            return _ret
        return join(odir, 'final')

    def _get_tandem_sam_fns():
        if args['keep_intermediates']:
            return join(odir, 'tandem_unp.sam'), join(odir, 'tandem_paired.sam'), join(odir, 'tandem_both.sam'), _nop
        else:
            dr = temp_man.get_dir('tandem_alignments')

            def _purge():
                temp_man.remove_group('tandem_alignments')
            return join(dr, 'unp.sam'), join(dr, 'paired.sam'), join(dr, 'both.sam'), _purge

    def _get_passthrough_args(exe):
        op = Popen(exe, stdout=PIPE).communicate()[0]
        ls = []
        for ar in op.strip().split(' '):
            ar_underscore = ar.replace('-', '_')
            if ar_underscore in args:
                logging.debug('  passing through argument "%s"="%s"' % (ar, str(args[ar_underscore])))
                ls.append(ar)
                ls.append(str(args[ar_underscore]))
        return ' '.join(ls)

    def _wait_for_aligner(_al):
        while _al.pipe.poll() is None:
            time.sleep(0.5)

    def _exists_and_nonempty(_fn):
        return os.path.exists(_fn) and os.stat(_fn).st_size > 0

    def _have_unpaired_tandem_reads(prefix):
        ufn = prefix + '_reads_u.fastq'
        return _exists_and_nonempty(ufn)

    def _have_paired_tandem_reads(prefix):
        cfn = prefix + '_reads_c_1.fastq'
        dfn = prefix + '_reads_d_1.fastq'
        bfn = prefix + '_reads_b_1.fastq'
        return _exists_and_nonempty(cfn) or _exists_and_nonempty(dfn) or _exists_and_nonempty(bfn)

    def _unpaired_tandem_reads(prefix):
        ufn = prefix + '_reads_u.fastq'
        return [ufn] if _exists_and_nonempty(ufn) else []

    def _paired_tandem_reads(prefix, single_file=False):
        ls = []
        cfn1, cfn2 = prefix + '_reads_c_1.fastq', prefix + '_reads_c_2.fastq'
        if _exists_and_nonempty(cfn1):
            ls.append((cfn1, cfn2))
        dfn1, dfn2 = prefix + '_reads_d_1.fastq', prefix + '_reads_d_2.fastq'
        if _exists_and_nonempty(dfn1):
            ls.append((dfn1, dfn2))
        bfn1, bfn2 = prefix + '_reads_b_1.fastq', prefix + '_reads_b_2.fastq'
        if _exists_and_nonempty(bfn1):
            ls.append((bfn1, bfn2))
        if len(ls) > 1 and single_file:
            fn1, fn2 = prefix + '_reads_combined_1.fastq', prefix + '_reads_combined_2.fastq'
            _cat(map(itemgetter(0), ls), fn1)
            _cat(map(itemgetter(1), ls), fn2)
            ls = [(fn1, fn2)]
        return ls

    # ##################################################
    # 1. Align input reads
    # ##################################################

    tim.start_timer('Aligning input reads')
    input_sam_fn, input_sam_purge = _get_input_sam_fn()
    logging.info('Command for aligning input data: "%s"' % align_cmd)
    aligner = aligner_class(
        align_cmd,
        aligner_args,
        aligner_unpaired_args,
        aligner_paired_args,
        args['index'],
        unpaired=args['U'],
        paired=None if args['m1'] is None else zip(args['m1'], args['m2']),
        sam=input_sam_fn)

    logging.debug('  waiting for aligner to finish...')
    _wait_for_aligner(aligner)
    logging.debug('  aligner finished; results in "%s"' % input_sam_fn)
    tim.end_timer('Aligning input reads')

    if not _at_least_one_read_aligned(input_sam_fn):
        logging.warning("None of the input reads aligned; exiting")
        sys.exit(0)

    # ##################################################
    # 2. Parse input SAM
    # ##################################################

    tim.start_timer('Parsing input alignments')
    sanity_check_binary(parse_input_exe)
    pass1_prefix, pass1_cleanup = _get_pass1_file_prefix()
    cmd = "%s ifs -- %s -- %s -- %s -- %s" % \
          (parse_input_exe, _get_passthrough_args(parse_input_exe), input_sam_fn, ' '.join(args['ref']), pass1_prefix)
    logging.info('  running "%s"' % cmd)
    ret = os.system(cmd)
    if ret != 0:
        raise RuntimeError("qsim-parse returned %d" % ret)
    logging.debug('  parsing finished; results in "%s.*"' % pass1_prefix)
    tim.end_timer('Parsing input alignments')

    # ##################################################
    # 3. Align tandem reads
    # ##################################################

    tim.start_timer('Aligning tandem reads')
    assert _have_unpaired_tandem_reads(pass1_prefix) or _have_paired_tandem_reads(pass1_prefix)
    tandem_sam_u_fn, tandem_sam_p_fn, tandem_sam_b_fn, tandem_purge = _get_tandem_sam_fns()
    if _have_unpaired_tandem_reads(pass1_prefix) and _have_paired_tandem_reads(pass1_prefix) and aligner.supports_mix():
        logging.info('Aligning tandem reads (mix)')
        aligner = aligner_class(
            align_cmd,
            aligner_args,
            aligner_unpaired_args,
            aligner_paired_args,
            args['index'],
            unpaired=_unpaired_tandem_reads(pass1_prefix),
            paired=_paired_tandem_reads(pass1_prefix, single_file=True),
            sam=tandem_sam_b_fn,
            input_format='fastq')
        _wait_for_aligner(aligner)
        logging.debug('Finished aligning unpaired and paired-end tandem reads')
    else:
        if _have_unpaired_tandem_reads(pass1_prefix):
            logging.info('Aligning tandem reads (unpaired)')
            aligner = aligner_class(
                align_cmd,
                aligner_args,
                aligner_unpaired_args,
                aligner_paired_args,
                args['index'],
                unpaired=_unpaired_tandem_reads(pass1_prefix),
                sam=tandem_sam_u_fn,
                input_format='fastq')
            _wait_for_aligner(aligner)
            logging.debug('Finished aligning unpaired tandem reads')
        if _have_paired_tandem_reads(pass1_prefix):
            logging.info('Aligning tandem reads (paired)')
            aligner = aligner_class(
                align_cmd,
                aligner_args,
                aligner_unpaired_args,
                aligner_paired_args,
                args['index'],
                paired=_paired_tandem_reads(pass1_prefix, single_file=True),
                sam=tandem_sam_p_fn,
                input_format='fastq')
            _wait_for_aligner(aligner)
            logging.debug('Finished aligning paired tandem reads')
    tandem_sams = filter(_exists_and_nonempty, [tandem_sam_u_fn, tandem_sam_p_fn, tandem_sam_b_fn])
    if len(tandem_sams) == 0:
        raise RuntimeError('No tandem reads written')
    tim.end_timer('Aligning tandem reads')

    # ##################################################
    # 4. Parse tandem alignments
    # ##################################################

    tim.start_timer('Parsing tandem alignments')
    sanity_check_binary(parse_input_exe)
    pass2_prefix, pass2_cleanup = _get_pass2_file_prefix()
    cmd = "%s f -- %s -- %s -- %s -- %s" % \
          (parse_input_exe, _get_passthrough_args(parse_input_exe),
           ' '.join(tandem_sams), ' '.join(args['ref']), pass2_prefix)
    logging.info('  running "%s"' % cmd)
    ret = os.system(cmd)
    if ret != 0:
        raise RuntimeError("qsim-parse returned %d" % ret)
    logging.debug('  parsing finished; results in "%s.*"' % pass2_prefix)
    tandem_purge()  # delete tandem-alignment intermediates
    tim.end_timer('Parsing tandem alignments')

    # ##################################################
    # 5. Predict
    # ##################################################

    tim.start_timer('Make MAPQ predictions')
    predictions_fn, purge_predictions = _get_predictions_fn()
    logging.info('Making MAPQ predictions')
    logging.info('  instantiating feature table readers')
    tab_ts, tab_tr = FeatureTableReader(pass1_prefix, chunksize=args['max_rows']), \
                     FeatureTableReader(pass2_prefix, chunksize=args['max_rows'])

    def _do_predict(fit, tab, subdir, is_input):
        pred = fit.predict(tab, temp_man, dedup=not args['no_collapse'], training=not is_input,
                           calc_summaries=args['assess_accuracy'], prediction_mem_limit=args['assess_limit'])
        if args['vanilla_output'] is None and pred.can_assess():
            logging.info('  writing accuracy measures')
            mkdir_quiet(join(*subdir))
            assert pred.ordered_by == 'pcor', pred.ordered_by
            pred.write_rocs(join(*(subdir + ['roc'])), join(*(subdir + ['cid'])), join(*(subdir + ['csed'])))
            pred.write_top_incorrect(join(*(subdir + ['top_incorrect.csv'])))
            pred.write_summary_measures(join(*(subdir + ['summary.csv'])))
            pred.order_by_ids()
            assert pred.ordered_by == 'id', pred.ordered_by
            logging.info('  writing predictions')
            pred.write_predictions(join(*(subdir + ['predictions.csv'])))
        if is_input:
            logging.info('  writing predictions')
            pred.write_predictions(predictions_fn)
        pred.purge_temporaries()  # purge temporary files generated during prediction
        return pred

    def _do_fit(_tab_tr, fam, fraction, subdir):
        fit = MapqFit(_tab_tr, fam, sample_fraction=fraction)
        if args['vanilla_output'] is None:
            mkdir_quiet(join(*subdir))
            fit.write_feature_importances(join(*(subdir + ['featimport'])))
            fit.write_parameters(join(*(subdir + ['params'])))
        return fit

    def _fits_and_predictions(fraction, fam, subdir):
        logging.info('  fitting to tandem alignments')
        subdir_ts = subdir + ['test'] if args['predict_for_training'] else subdir
        fit = _do_fit(tab_tr, fam, fraction, subdir)
        logging.info('  making predictions for input alignments')
        _do_predict(fit, tab_ts, subdir_ts, True)
        if args['predict_for_training']:
            logging.info('  making predictions for tandem (training) alignments')
            _do_predict(fit, tab_tr, subdir + ['training'], False)

    def _all_fits_and_predictions():
        fractions = map(float, args['subsampling_series'].split(','))
        for fraction in fractions:
            if fraction < 0.0 or fraction > 1.0:
                raise RuntimeError('Bad subsampling fraction: %f' % fraction)
            _odir = [odir, 'sample' + str(fraction)] if len(fractions) > 1 else [odir]
            if len(fractions) > 1:
                logging.info('  trying sampling fraction %0.02f%%' % (100.0 * fraction))
            for i in range(args['trials']):
                if args['trials'] > 1:
                    logging.info('  trial %d' % (i+1))
                seed = args['seed'] + i
                seed_all(seed)
                logging.info('  pseudo-random seed %d' % seed)
                fam = model_family(args['model_family'], seed, args['optimization_tolerance'])
                _fits_and_predictions(fraction, fam, _odir + ['trial%d' % (i+1)] if args['trials'] > 1 else _odir)

    _all_fits_and_predictions()
    pass1_cleanup()
    pass2_cleanup()
    tim.end_timer('Make MAPQ predictions')

    # ##################################################
    # 6. Rewrite SAM
    # ##################################################

    tim.start_timer('Rewrite SAM file')
    sanity_check_binary(rewrite_exe)
    pass3_prefix, pass3_cleanup = _get_pass3_file_prefix()
    cmd = "%s %s -- %s -- %s -- %s" % \
          (rewrite_exe, _get_passthrough_args(rewrite_exe), input_sam_fn, predictions_fn, _get_final_prefix())
    logging.info('  running "%s"' % cmd)
    ret = os.system(cmd)
    if ret != 0:
        raise RuntimeError("qsim-rewrite returned %d" % ret)
    pass3_cleanup()
    logging.debug('  rewriting finished; results in "%s.*"' % pass3_prefix)
    input_sam_purge()
    purge_predictions()
    tim.end_timer('Rewrite SAM file')

    logging.info('Purging temporaries')
    temp_man.purge()

    out_sz = getsize(_get_final_prefix() + '.sam')
    peak_tmp = temp_man.peak_size
    logging.info('Output SAM size: %0.2fMB' % (out_sz / (1024.0 * 1024)))
    logging.info('Peak temporary file size: %0.2fMB (%0.2f%% of output SAM)' % (peak_tmp / (1024.0 * 1024),
                                                                                100.0 * peak_tmp / out_sz))
    if args['vanilla_output'] is None:
        tot_sz = recursive_size(odir)
        logging.info('Total size of output directory: %0.2fMB (%0.2f%% of output SAM)' % (tot_sz / (1024.0 * 1024),
                                                                                          100.0 * tot_sz / out_sz))

    logging.info('Peak memory usage (RSS) of Python wrapper: %0.2fGB' %
                 (resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024.0 * 1024.0)))
    logging.info('Peak memory usage (RSS) of children: %0.2fGB' %
                 (resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss / (1024.0 * 1024.0)))

    tim.end_timer('Overall')
    for ln in str(tim).split('\n'):
        if len(ln) > 0:
            logging.info(ln)
    if not args['vanilla_output']:
        with open(join(odir, 'timing.tsv'), 'w') as fh:
            fh.write(str(tim))
    logging.info('Time overhead: %0.01f%%' % (100.0 * (tim.timers['Overall'] - tim.timers['Aligning input reads']) /
                                              tim.timers['Aligning input reads']))
    # TODO: Memory overhead?


def add_args(parser):

    # Overall arguments
    parser.add_argument('--ref', metavar='path', type=str, nargs='+', required=True,
                        help='FASTA file(s) containing reference genome sequences')
    parser.add_argument('--U', metavar='path', type=str, nargs='+', help='Unpaired read files')
    parser.add_argument('--m1', metavar='path', type=str, nargs='+',
                        help='Mate 1 files; must be specified in same order as --m2')
    parser.add_argument('--m2', metavar='path', type=str, nargs='+',
                        help='Mate 2 files; must be specified in same order as --m1')
    parser.add_argument('--index', metavar='path', type=str, help='Index file to use (usually a prefix).')
    parser.add_argument('--seed', metavar='int', type=int, default=99099, required=False,
                        help='Integer to initialize pseudo-random generator')

    # Qsim-parse: input model
    parser.add_argument('--max-allowed-fraglen', metavar='int', type=int, default=100000, required=False,
                        help='When simulating fragments, observed fragments longer than this will be'
                             'truncated to this length')
    parser.add_argument('--input-model-size', metavar='int', type=int, default=30000, required=False,
                        help='Number of templates to keep when building input model.')
    # unused
    """
    parser.add_argument('--low-score-bias', metavar='float', type=float, default=1.0, required=False,
                        help='When simulating reads, we randomly select a real read\'s alignment profile'
                             'as a template.  A higher value for this parameter makes it more likely'
                             'we\'ll choose a low-scoring alignment.  If set to 1, all templates are'
                             'equally likely.')
    parser.add_argument('--fraction-even', metavar='float', type=float, default=1.0, required=False,
                        help='Fraction of the time to sample templates from the unstratified input '
                             'sample versus the stratified sample.')
    """

    # Qsim-parse: simulator
    parser.add_argument('--sim-fraction', metavar='fraction', type=float, default=0.03, required=False,
                        help='When determining the number of simulated reads to generate for each type of '
                             'alignment (concordant, discordant, bad-end, unpaired), let it be no less '
                             'than this fraction times the number of alignment of that type in the input '
                             'data.')
    parser.add_argument('--sim-unp-min', metavar='int', type=int, default=30000, required=False,
                        help='Number of simulated unpaired reads will be no less than this number.')
    parser.add_argument('--sim-conc-min', metavar='int', type=int, default=30000, required=False,
                        help='Number of simulated concordant pairs will be no less than this number.')
    parser.add_argument('--sim-disc-min', metavar='int', type=int, default=10000, required=False,
                        help='Number of simulated discordant pairs will be no less than this number.')
    parser.add_argument('--sim-bad-end-min', metavar='int', type=int, default=10000, required=False,
                        help='Number of simulated pairs with-one-bad-end will be no less than this number.')

    # Qsim-parse: correctness
    parser.add_argument('--wiggle', metavar='int', type=int, default=30, required=False,
                        help='Wiggle room to allow in starting position when determining whether alignment is correct')

    # Aligner
    parser.add_argument('--bt2-exe', metavar='path', type=str, help='Path to Bowtie 2 exe')
    parser.add_argument('--bwa-exe', metavar='path', type=str, help='Path to BWA exe')
    parser.add_argument('--snap-exe', metavar='path', type=str, help='Path to snap-aligner exe')
    parser.add_argument('--aligner', metavar='name', default='bowtie2', type=str,
                        help='bowtie2 | bwa-mem | snap')

    # SAM rewriting
    parser.add_argument('--orig-mapq-flag', metavar='XX:X', type=str, default="Zm:Z", required=False,
                        help='Extra field label for original MAPQ; see SAM spec for formatting')
    parser.add_argument('--precise-mapq-flag', metavar='XX:X', type=str, default="Zp:Z", required=False,
                        help='Extra field label for original MAPQ; see SAM spec for formatting')
    parser.add_argument('--write-orig-mapq', action='store_const', const=True, default=False,
                        help='Write original MAPQ as an extra field in output SAM')
    parser.add_argument('--write-precise-mapq', action='store_const', const=True, default=False,
                        help='Write a more precise MAPQ prediction as an extra field in output SAM')
    parser.add_argument('--keep-ztz', action='store_const', const=True, default=False,
                        help='Don\'t remove the ZT:Z flag from the final output SAM')

    # Prediction
    parser.add_argument('--model-family', metavar='family', type=str, required=False,
                        default='ExtraTrees', help='{RandomForest | ExtraTrees}')
    parser.add_argument('--optimization-tolerance', metavar='float', type=float, default=1e-3,
                        help='Tolerance when searching for best model parameters')
    parser.add_argument('--predict-for-training', action='store_const', const=True, default=False,
                        help='Make predictions (and associated plots/output files) for training (tandem) data')
    parser.add_argument('--no-collapse', action='store_const', const=True, default=False,
                        help='Don\'t remove redundant rows just before prediction')
    parser.add_argument('--subsampling-series', metavar='floats', type=str, default='1.0',
                        help='Comma separated list of subsampling fractions to try')
    parser.add_argument('--trials', metavar='int', type=int, default=1,
                        help='Number of times to repeat fiting/prediction')
    parser.add_argument('--max-rows', metavar='int', type=int, default=500000,
                        help='Maximum number of rows (alignments) to feed at once to the prediction function')
    parser.add_argument('--profile-prediction', action='store_const', const=True, default=False,
                        help='Run a profiler for the duration of the prediction portion')

    # Assessment of prediction accuracy
    parser.add_argument('--assess-accuracy', action='store_const', const=True, default=False,
                        help='When correctness can be inferred from simulated read names, '
                             'assess accuracy of old and new MAPQ predictions')
    parser.add_argument('--assess-limit', metavar='int', type=int, default=100000000,
                        help='The maximum number of alignments to assess when assessing accuracy')

    # Output file-related arguments
    parser.add_argument('--temp-directory', metavar='path', type=str, required=False,
                        help='Write temporary files to this directory; default: uses environment variables '
                             'like TMPDIR, TEMP, etc')
    parser.add_argument('--output-directory', metavar='path', type=str,
                        help='Write outputs to this directory')
    parser.add_argument('--vanilla-output', metavar='path', type=str,
                        help='Only write final SAM file; suppress all other output')
    parser.add_argument('--keep-intermediates', action='store_const', const=True, default=False,
                        help='Keep intermediates in output directory; if not specified, '
                             'intermediates are written to a temporary directory then deleted')


def go_profile(args, aligner_args, aligner_unpaired_args, aligner_paired_args):
    if args['profile']:
        import cProfile
        cProfile.run('go(args, aligner_args, aligner_unpaired_args, aligner_paired_args)')
    else:
        go(args, aligner_args, aligner_unpaired_args, aligner_paired_args)


def parse_aligner_parameters_from_argv(argv):
    argv = argv[:]
    sections = [[]]
    for arg in argv:
        if arg == '--':
            sections.append([])
        else:
            sections[-1].append(arg)
    new_argv = sections[0]
    aligner_args = [] if len(sections) < 2 else sections[1]
    aligner_unpaired_args = [] if len(sections) < 3 else sections[2]
    aligner_paired_args = [] if len(sections) < 4 else sections[3]
    return new_argv, aligner_args, aligner_unpaired_args, aligner_paired_args


if __name__ == "__main__":
    
    import argparse

    _parser = argparse.ArgumentParser(
        description='Align a collection of input reads, simulate a tandem'
                    'dataset, align the tandem dataset, and emit both the'
                    'input read alignments and the training data derived from'
                    'the tandem read alignments.')

    if '--version' in sys.argv:
        print('Tandem simulator, version ' + VERSION)
        sys.exit(0)

    add_args(_parser)

    # Some basic flags
    _parser.add_argument('--profile', action='store_const', const=True, default=False, help='Print profiling info')
    _parser.add_argument('--verbose', action='store_const', const=True, default=False, help='Be talkative')
    _parser.add_argument('--version', action='store_const', const=True, default=False, help='Print version and quit')

    _argv, _aligner_args, _aligner_unpaired_args, _aligner_paired_args = parse_aligner_parameters_from_argv(sys.argv)
    _args = _parser.parse_args(_argv[1:])

    go_profile(vars(_args), _aligner_args, _aligner_unpaired_args, _aligner_paired_args)
