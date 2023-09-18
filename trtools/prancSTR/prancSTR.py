#!/usr/bin/env python3
"""
Performs detection of somatic variation at STRs
"""

import argparse
import numpy as np
import os
import sys
import time

from scipy.optimize import minimize
from scipy.stats.distributions import chi2

import trtools.utils.utils as utils
import trtools.utils.common as common
import trtools.utils.tr_harmonizer as trh
from trtools import __version__

READFIELD = "MALLREADS"
ZERO = 10e-200
MAXSTUTTEROFFSET = 100

def StutterProb(delta, stutter_u, stutter_d, stutter_rho):
    r"""Compute P(r_i | genotype; error model)

    Parameters
    ----------
    delta : int
       Difference in repeat length between observed
       and underlying allele, given in copy number
       (r_i-genotype)
    stutter_u : float
       Probability to see an expansion stutter error
    stutter_d : float
       Probability to see a deletion stutter error
    stutter_rho : float
       Step size parameter

    Returns
    -------
    prob : float
       P(r_i|genotype)
    """
    abs_delta = abs(delta)
    if delta == 0:
        prob = 1 - stutter_u - stutter_d
    elif delta > 0:
        prob = (stutter_u)*(stutter_rho)*(pow((1-stutter_rho), (abs_delta-1)))
    elif delta < 0:
        prob = (stutter_d)*(stutter_rho)*(pow((1-stutter_rho), (abs_delta-1)))
    return prob

def MaximizeMosaicLikelihoodBoth(reads, A, B,
                                 stutter_probs,
                                 maxiter=100, locname="None",
                                 quiet=False):
    r"""Find the maximum likelihood values of 
    C: mosaic allele
    f: mosaic fraction

    Parameters
    ----------
    reads : list of int
       list of repeat lengths seen in each read
    A : int
       First allele of the genotype
    B : int
       Second allele of the genotype
    stutter_probs : list of floats
       stutter probs for each delta
    max_iter : int (optional)
       Maximum number of iterations to run
       the estimation procedure. Default=100
    locname : str (optional)
       String identifier of the locus. For
       warning message purposes. Default: "None"
    quiet : bool
       Don't print out any messages
    Returns
    -------
    C : int
       Estimated mosaic allele
    f : float
       Estimated mosaic fraction
    """
    # Initialize to reasonable values
    f = 0.01
    c_prev = 0
    f_prev = 0

    # First predict C and F separately
    C = Just_C_Pred(reads, A, B, f, stutter_probs)
    f = Just_F_Pred(reads, A, B, C, stutter_probs)

    # Iterate between predicting C and F
    iter_num = 1
    while True:
        c_prev = C
        f_prev = f
        # calling function for predicting mosaic allele value
        C = Just_C_Pred(reads, A, B, f, stutter_probs)
        # calling function for predicting mosaic fraction value
        f = Just_F_Pred(reads, A, B, C, stutter_probs)
        iter_num += 1
        if iter_num > maxiter:
            if not quiet: common.WARNING("ML didn't converge reads=%s A=%s B=%s %s" %
                           (str(reads), A, B, locname))
            break
        if abs(f-f_prev) < 0.01 and (f < 0.000001 or C == c_prev):
            break  # checking for and preventing convergence
    if f == 0.0:
        C = None  # stating C as None when the mosaic allele fraction is 0
    return C, f

def Just_C_Pred(reads, A, B, f, stutter_probs):
    r"""Predict C, holding f constant

    Parameters
    ----------
    reads : list of int
       list of repeat lengths seen in each read
    A : int
       First allele of the genotype
    B : int
       Second allele of the genotype
    f : float
       Mosaic fraction
    stutter_probs : list of floats
       stutter probs for each delta

    Returns
    -------
    C : int
       mosaic allele
    """
    min_limit = min(reads)-3
    # max range is 3 above the min of the set of reads
    max_limit = max(reads)+3
    c_range = [i for i in range(min_limit, max_limit+1)]
    max_likehood = float("-inf")

    def Likelihood_mosaic_C(c_value):
        return Likelihood_mosaic(A, B, c_value, f, reads,
                                 stutter_probs)
    c_final = 0
    for i in c_range:
        log_likehood = Likelihood_mosaic_C(i)
        if max_likehood < log_likehood:
            max_likehood = log_likehood
            c_final = i
    return c_final


def Just_F_Pred(reads, A, B, C, stutter_probs):
    r"""Predict f, holding C constant

    Parameters
    ----------
    reads : list of int
       list of repeat lengths seen in each read
    A : int
       First allele of the genotype
    B : int
       Second allele of the genotype
    C : integer
       Mosaic allele
    stutter_probs : list of floats
       stutter probs for each delta

    Returns
    -------
    f : float
       mosaic fraction
    """

    def Likelihood_mosaic_f(f):
        return (-Likelihood_mosaic(A, B, C, f[0], reads,
                                   stutter_probs))

    f_initial = np.array([0.01])
    bound_var = ((0, 0.5),) ##changed the bounds to be more accurate of what it could be in data. 0.5 makes the most sense so far
    result = minimize(Likelihood_mosaic_f, f_initial,
                      method="SLSQP", options={}, bounds=bound_var)
    f_final = result.x
    return f_final[0]


def ExtractAB(trrecord):
    r"""
    Extract list of <A,B> for each sample
    Parameters
    ----------
    trrecord: trh.TRRecord
       TRRecord object for the locus

    Returns
    -------
    genotypes : list of list of ints
       [(A,B), ..] genotypes for each sample
       given in terms of bp diff from ref
    """
    full_gts = trrecord.GetStringGenotypes()
    reflen = len(trrecord.ref_allele)
    called = trrecord.GetCalledSamples()
    genotypes = []
    for i in range(len(full_gts)):
        item = full_gts[i]
        if not called[i]:
            genotypes.append([None, None])
        else:
            genotypes.append([int((len(item[0])-reflen)),
                              int((len(item[1])-reflen))])
    return genotypes


def ExtractReadVector(mallreads, period):
    r"""
    Extract reads vector from MALLREADS
    MALLREADS has format: allele1|readcount1;allele2|readcount2...
    Paramters
    ---------
    mallreads : str
        MALLREADS string from HipSTR output
    period : int
        STR unit length
    
    Returns
    -------
    reads : list of int
        List with one entry per read. 
        Given in terms of difference in repeats
        from reference
    """
    reads = []
    if mallreads is None:
        return reads
    for allele_data in mallreads.split(";"):
        if "|" not in allele_data:
            break
        al, count = allele_data.split("|")
        al = int(al)//period
        count = int(count)
        reads.extend([int(al)]*count)
    return reads

def ConfineRange(x, minval, maxval):
	r"""Confine the range of a nmber to lie
	between minval and maxval

	Parameters
	----------
	x : numeric
	   The value to be constrained
	minval : numeric
	   The minimum value the output can take
	maxval : numeric
	   The maximum value the output can take

	Returns
	-------
	x_cons : numeric
	   New value, which cannot exceed maxval
	   or go below minval
	"""
	x_cons = x
	if x < minval:
		x_cons = minval
	if x > maxval:
		x_cons = maxval
	return x_cons

def Likelihood_mosaic(A, B, C, f, reads, stutter_probs):
    r"""
    Compute likelihood of observing the reads, given
    true genotype=A,B and mosaic allele C, mosaic fraction f

    Parameters
    ----------
    reads : list of int
       list of repeat lengths seen in each read
    A : int
       First allele of the genotype
    B : int
       Second allele of the genotype
    C : integer
       Mosaic allele
    stutter_probs : list of floats
       stutter probs for each delta
    f : float
       mosaic fraction
    
    Returns
    -------
    sum_likelihood : float
        sum of max likelihood calculated for each read
    """
    # Get dictionary of read counts
    # e.g. [-10, -10, -10, 3, 3, 3, 3] -> {-10: 3, 3: 4}
    rcounts = {} # r -> num. times observed
    for r in set(reads):
        rcounts[r] = reads.count(r)

    # Compute likelihood
    # Assume C should completely come from either A or B
    # Consider both cases (A=1/2, B=1/2-f, C=f) or
    # (A=1/2-f, B=1/2, C=f)

    sum_likelihood_1 = 0
    sum_likelihood_2 = 0
    for r in rcounts.keys():
        delta_A = ConfineRange(r-A, -100, 100)
        delta_B = ConfineRange(r-B, -100, 100)

        count = rcounts[r]
        if C in [A, B]:
            like_li_hood_1 = ZERO
            like_li_hood_2 = ZERO
        else:
            if C is None:
                C = 0
                delta_C = 0 # C is None when the mosaic allele fraction is 0
            else:
                delta_C = ConfineRange(r-C, -100, 100)
            like_li_hood_1 = (1/2)*stutter_probs[delta_A+MAXSTUTTEROFFSET] + \
                           ((1/2)-f)*stutter_probs[delta_B+MAXSTUTTEROFFSET] + \
                           (f)*stutter_probs[delta_C+MAXSTUTTEROFFSET]
            like_li_hood_2 = ((1/2)-f)*stutter_probs[delta_A+MAXSTUTTEROFFSET] + \
                           (1/2)*stutter_probs[delta_B+MAXSTUTTEROFFSET] + \
                           (f)*stutter_probs[delta_C+MAXSTUTTEROFFSET]
        
        sum_likelihood_1 = sum_likelihood_1 + count*np.log(like_li_hood_1)
        sum_likelihood_2 = sum_likelihood_2 + count*np.log(like_li_hood_2)

    sum_likelihood = max(sum_likelihood_1, sum_likelihood_2)
    return sum_likelihood

def SF(x):
	r"""Survival function of a point mass at 0

	Parameters
	----------
	x : float
	   Observed value

	Returns
	-------
	sf : float
	   Survival function result
	"""
	if x > 0: sf = 0
	if x <= 0: sf = 1
	return sf

def ComputePvalue(reads, A, B, best_C, best_f, stutter_probs):
	r"""Compute pvalue testing H0: f=0

	Parameters
	----------
    reads : list of int
       list of repeat lengths seen in each read
    A : int
       First allele of the genotype
    B : int
       Second allele of the genotype
    best_C : integer
       Estimated mosaic allele
    best_f : float
       mosaic fraction
    stutter_probs : list of floats
       stutter probs for each delta

    Returns
    -------
    pval : float
       P-value testing H0: f=0
	"""
	log_obs = Likelihood_mosaic(A, B, best_C, best_f, reads, stutter_probs)
	log_exp = Likelihood_mosaic(A, B, best_C, 0, reads, stutter_probs)
	test_stat = -2*(log_exp-log_obs)
	pval = 0.5*SF(test_stat) + 0.5*chi2.sf(test_stat, 2)
	#pval = 1 - scipy.stats.chi2.cdf(test_stat, 1)
	return pval

def getargs():
    parser = argparse.ArgumentParser(
        __doc__,
        formatter_class=utils.ArgumentDefaultsHelpFormatter
    )
    inout_group = parser.add_argument_group("Input/output")
    inout_group.add_argument(
        "--vcf", help="Input STR VCF file", type=str, required=True)
    inout_group.add_argument(
        "--out", help=("Output file prefix. Use stdout to print file to standard output"), type=str, required=True)
    inout_group.add_argument("--vcftype", help="Options=%s" %
                             [str(item) for item in trh.VcfTypes.__members__], type=str, default="auto")
    inout_group.add_argument("--samples", help="Comma-separated list of samples to process", type=str)
    filter_group = parser.add_argument_group("Filtering group")
    filter_group.add_argument("--region", help="Restrict to the region "
                              "chrom:start-end. Requires file to bgzipped and"
                              " tabix indexed.", type=str)
    filter_group.add_argument("--only-passing", help="Only process records "
                              " where FILTER==PASS", action="store_true")
    filter_group.add_argument("--output-all", help="Force output results for all loci", action="store_true")
    other_group = parser.add_argument_group("Other options")
    other_group.add_argument(
        "--debug", help="Print helpful debug messages", action="store_true")
    other_group.add_argument(
    	"--quiet", help="Don't print messages to the screen", action="store_true")
    ver_group = parser.add_argument_group("Version")
    ver_group.add_argument("--version", action="version",
                           version='{version}'.format(version=__version__))
    args = parser.parse_args()
    return args

def main(args):
    if not os.path.exists(args.vcf):
        common.WARNING("Error: {} does not exist".format(args.vcf))
        return 1

    if not os.path.exists(os.path.dirname(os.path.abspath(args.out))):
        common.WARNING("Error: The directory which contains the output location {} does"
                       " not exist".format(args.out))
        return 1

    if os.path.isdir(args.out) and args.out.endswith(os.sep):
        common.WARNING("Error: The output location {} is a "
                       "directory".format(args.out))
        return 1

    checkgz = args.region is not None
    invcf = utils.LoadSingleReader(args.vcf, checkgz=checkgz)
    samples = invcf.samples
    if invcf is None:
        return 1
    if args.vcftype != 'auto':
        vcftype = trh.VcfTypes[args.vcftype]
    else:
        vcftype = trh.InferVCFType(invcf)
    if vcftype != trh.VcfTypes.hipstr:
        common.WARNING("Error: Only HipSTR VCFs currently supported "
                       " by prancSTR")
        return 1

    if args.region:
        region = invcf(args.region)
    else:
        region = invcf

    usesamples = []
    if args.samples is not None:
        usesamples = args.samples.split(",")

    start_time = time.time()
    nrecords = 0

    try:
        if args.out == "stdout":
            outf = sys.stdout
        else:
            outf = open(args.out + ".tab", "w")

        # Header
        header_cols = ["sample", "chrom", "pos", "locus", "motif",
                       "A", "B", "C", "f", "pval", "reads",
                       "mosaic_support", "stutter parameter u",
                       "stutter paramter d", "stutter paramter rho",
                       "quality factor", "read depth"]
        outf.write("\t".join(header_cols)+"\n")

        for record in region:
            nrecords += 1
            trrecord = trh.HarmonizeRecord(vcftype, record)

            if args.only_passing and not args.output_all and (record.FILTER is not None):
                common.WARNING("Skipping non-passing record %s" %
                               str(trrecord))
                continue

            ########### Extract necessary info from the VCF file #######
            # Stutter params for the locus. These are the same for all samples
            # First check we have all the fields we need
            if READFIELD not in trrecord.format.keys():
                common.WARNING("Could not find MALLREADS for %s" %
                               str(trrecord))
                continue
            if "INFRAME_UP" not in trrecord.info.keys() or \
                "INFRAME_DOWN" not in trrecord.info.keys() or \
                    "INFRAME_PGEOM" not in trrecord.info.keys():
                common.WARNING(
                    "Could not find stutter info for %s" % str(trrecord))
                continue
            stutter_u = trrecord.info["INFRAME_UP"]
            stutter_d = trrecord.info["INFRAME_DOWN"]
            stutter_rho = trrecord.info["INFRAME_PGEOM"]
            if stutter_u == 0.0:
                stutter_u = 0.01
            if stutter_d == 0.0:
                stutter_d = 0.01
            if stutter_rho == 1.0:
                stutter_rho = 0.95
            stutter_probs = [StutterProb(d, stutter_u, stutter_d, stutter_rho) \
                for d in range(-MAXSTUTTEROFFSET, MAXSTUTTEROFFSET)]
            period = len(trrecord.motif)

            # Array of (A,B) for each sample
            # given in bp diff from ref
            # these get converted to repeat units below
            genotypes = ExtractAB(trrecord)

            # Array of "reads" vectors for each sample
            # given in repeat units diff from ref
            mallreads = [ExtractReadVector(item, period)
                         for item in trrecord.format[READFIELD]]

            # Extracting quality parameter
            Q = trrecord.format['Q']

            # Extracting depth parameter DP
            DP = trrecord.format['DP']

            ########### Run detection on each sample #######
            for i in range(len(samples)):
                if args.samples is not None and samples[i] not in usesamples: continue
                reads = mallreads[i]
                A, B = genotypes[i]
                q = Q[i][0]
                dp = DP[i][0]
                # for cases where there is no DP and it gets picked up as a random negative number
                if dp < 0:
                    dp = 0
                if A is None or B is None or len(reads) == 0:
                    continue  # skip locus if not called
                A, B = A//period, B//period
                if args.debug:
                    common.WARNING("Checking mosaicism for sample %s at %s" % (
                        samples[i], str(trrecord)))
                    common.WARNING("A=%s B=%s reads=%s" % (A, B, str(reads)))

                # Discard locus if: no evidence for caleld genotypes
                if A not in reads or B not in reads and not args.output_all:
                    continue
                # Discard locus if: only a single allele seen in the reads
                if len(set(reads)) == 1 and not args.output_all:
                    continue

                locname = "%s:%s" % (record.CHROM, record.POS)
                best_C, best_f = MaximizeMosaicLikelihoodBoth(reads, A, B, stutter_probs,
                                                              locname=locname, quiet=args.quiet)
                pval = ComputePvalue(reads, A, B, best_C, best_f, stutter_probs)

                outf.write('\t'.join([samples[i], record.CHROM, str(record.POS),
                                      str(record.ID), trrecord.motif, str(
                                          A), str(B),
                                      str(best_C), str(best_f), str(pval),
                                      trrecord.format[READFIELD][i],
                                      str(reads.count(best_C)),
                                      str(stutter_u), str(
                                          stutter_d), str(stutter_rho),
                                      str(q), str(dp)]) + '\n')
                if args.debug:
                    common.WARNING("Inferred best_C=%s best_f=%s" %
                                   (best_C, best_f))
            #############################################################
            if args.out == "stdout" and nrecords % 50 == 0:
                common.MSG("Finished {} records, time/record={:.5}sec".format(nrecords,
                      (time.time() - start_time)/nrecords), debug=True)
    finally:
        if outf is not None and args.out != "stdout":
            outf.close()
    return 0

def run():  # pragma: no cover
    args = getargs()
    if args == None:
        sys.exit(1)
    else:
        retcode = main(args)
        sys.exit(retcode)

if __name__ == "__main__":  # pragma: no cover
    run()