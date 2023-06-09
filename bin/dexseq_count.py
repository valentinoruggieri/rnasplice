#!/usr/bin/env python
from __future__ import division
import sys, itertools, optparse, warnings

optParser = optparse.OptionParser(
    usage="python %prog [options] <flattened_gff_file> <alignment_file> <output_file>",
    description="This script counts how many reads in <alignment_file> fall onto each exonic "
    + "part given in <flattened_gff_file> and outputs a list of counts in "
    + "<output_file>, for further analysis with the DEXSeq Bioconductor package. "
    + "Notes: Use dexseq_prepare_annotation.py to produce <flattened_gff_file>. "
    + "<alignment_file> may be '-' to indicate standard input.",
    epilog="Written by Simon Anders (sanders@fs.tum.de) and Alejandro Reyes (reyes@embl.de), "
    + "European Molecular Biology Laboratory (EMBL). (c) 2010-2013. Released under the "
    + " terms of the GNU General Public License v3. Part of the 'DEXSeq' package.",
)

optParser.add_option(
    "-p",
    "--paired",
    type="choice",
    dest="paired",
    choices=("no", "yes"),
    default="no",
    help="'yes' or 'no'. Indicates whether the data is paired-end (default: no)",
)

optParser.add_option(
    "-s",
    "--stranded",
    type="choice",
    dest="stranded",
    choices=("yes", "no", "reverse"),
    default="yes",
    help="'yes', 'no', or 'reverse'. Indicates whether the data is "
    + "from a strand-specific assay (default: yes ). "
    + "Be sure to switch to 'no' if you use a non strand-specific RNA-Seq library "
    + "preparation protocol. 'reverse' inverts strands and is needed for certain "
    + "protocols, e.g. paired-end with circularization.",
)

optParser.add_option(
    "-a",
    "--minaqual",
    type="int",
    dest="minaqual",
    default=10,
    help="skip all reads with alignment quality lower than the given " + "minimum value (default: 10)",
)

optParser.add_option(
    "-f",
    "--format",
    type="choice",
    dest="alignment",
    choices=("sam", "bam"),
    default="sam",
    help="'sam' or 'bam'. Format of <alignment file> (default: sam)",
)

optParser.add_option(
    "-r",
    "--order",
    type="choice",
    dest="order",
    choices=("pos", "name"),
    default="name",
    help="'pos' or 'name'. Sorting order of <alignment_file> (default: name). Paired-end sequencing "
    + "data must be sorted either by position or by read name, and the sorting order "
    + "must be specified. Ignored for single-end data.",
)


if len(sys.argv) == 1:
    optParser.print_help()
    sys.exit(1)

(opts, args) = optParser.parse_args()

if len(args) != 3:
    sys.stderr.write(sys.argv[0] + ": Error: Please provide three arguments.\n")
    sys.stderr.write("  Call with '-h' to get usage information.\n")
    sys.exit(1)

try:
    import HTSeq
except ImportError:
    sys.stderr.write("Could not import HTSeq. Please install the HTSeq Python framework\n")
    sys.stderr.write("available from http://www-huber.embl.de/users/anders/HTSeq\n")
    sys.exit(1)

gff_file = args[0]
sam_file = args[1]
out_file = args[2]
stranded = opts.stranded == "yes" or opts.stranded == "reverse"
reverse = opts.stranded == "reverse"
is_PE = opts.paired == "yes"
alignment = opts.alignment
minaqual = opts.minaqual
order = opts.order

if alignment == "bam":
    try:
        import pysam
    except ImportError:
        sys.stderr.write("Could not import pysam, which is needed to process BAM file (though\n")
        sys.stderr.write("not to process text SAM files). Please install the 'pysam' library from\n")
        sys.stderr.write("https://code.google.com/p/pysam/\n")
        sys.exit(1)


if sam_file == "-":
    sam_file = sys.stdin


# Step 1: Read in the GFF file as generated by aggregate_genes.py
# and put everything into a GenomicArrayOfSets

features = HTSeq.GenomicArrayOfSets("auto", stranded=stranded)
for f in HTSeq.GFF_Reader(gff_file):
    if f.type == "exonic_part":
        f.name = f.attr["gene_id"].strip('"') + ":" + f.attr["exonic_part_number"].strip('"')
        features[f.iv] += f.name

# initialise counters
num_reads = 0
counts = {}
counts["_empty"] = 0
counts["_ambiguous"] = 0
counts["_lowaqual"] = 0
counts["_notaligned"] = 0
counts["_ambiguous_readpair_position"] = 0

# put a zero for each feature ID
for iv, s in features.steps():
    for f in s:
        counts[f] = 0


# We need this little helper below:
def reverse_strand(s):
    if s == "+":
        return "-"
    elif s == "-":
        return "+"
    else:
        raise SystemError("illegal strand")


def update_count_vector(counts, rs):
    if type(rs) == str:
        counts[rs] += 1
    else:
        for f in rs:
            counts[f] += 1
    return counts


def map_read_pair(af, ar):
    rs = set()
    if af and ar and not af.aligned and not ar.aligned:
        return "_notaligned"
    if af and ar and not af.aQual < minaqual and ar.aQual < minaqual:
        return "_lowaqual"
    if af and af.aligned and af.aQual >= minaqual and af.iv.chrom in list(features.chrom_vectors.keys()):
        for cigop in af.cigar:
            if cigop.type != "M":
                continue
            if reverse:
                cigop.ref_iv.strand = reverse_strand(cigop.ref_iv.strand)
            for iv, s in features[cigop.ref_iv].steps():
                rs = rs.union(s)
    if ar and ar.aligned and ar.aQual >= minaqual and ar.iv.chrom in list(features.chrom_vectors.keys()):
        for cigop in ar.cigar:
            if cigop.type != "M":
                continue
            if not reverse:
                cigop.ref_iv.strand = reverse_strand(cigop.ref_iv.strand)
            for iv, s in features[cigop.ref_iv].steps():
                rs = rs.union(s)
    set_of_gene_names = set([f.split(":")[0] for f in rs])
    if len(set_of_gene_names) == 0:
        return "_empty"
    elif len(set_of_gene_names) > 1:
        return "_ambiguous"
    else:
        return rs


def clean_read_queue(queue, current_position):
    clean_queue = dict(queue)
    for i in queue:
        if queue[i].mate_start.pos < current_position:
            warnings.warn(
                "Read " + i + " claims to have an aligned mate that could not be found in the same chromosome."
            )
            del clean_queue[i]
    return clean_queue


if alignment == "sam":
    reader = HTSeq.SAM_Reader
else:
    reader = HTSeq.BAM_Reader


# Now go through the aligned reads
num_reads = 0

if not is_PE:
    for a in reader(sam_file):
        if not a.aligned:
            counts["_notaligned"] += 1
            continue
        if "NH" in a.optional_fields and a.optional_field("NH") > 1:
            continue
        if a.aQual < minaqual:
            counts["_lowaqual"] += 1
            continue
        rs = set()
        for cigop in a.cigar:
            if cigop.type != "M":
                continue
            if reverse:
                cigop.ref_iv.strand = reverse_strand(cigop.ref_iv.strand)
            for iv, s in features[cigop.ref_iv].steps():
                rs = rs.union(s)
        set_of_gene_names = set([f.split(":")[0] for f in rs])
        if len(set_of_gene_names) == 0:
            counts["_empty"] += 1
        elif len(set_of_gene_names) > 1:
            counts["_ambiguous"] += 1
        else:
            for f in rs:
                counts[f] += 1
        num_reads += 1
        if num_reads % 100000 == 0:
            sys.stderr.write("%d reads processed.\n" % num_reads)

else:  # paired-end
    alignments = dict()
    if order == "name":
        for af, ar in HTSeq.pair_SAM_alignments(reader(sam_file)):
            if af == None or ar == None:
                continue
            if not ar.aligned:
                continue
            if not af.aligned:
                continue
            elif ar.optional_field("NH") > 1 or af.optional_field("NH") > 1:
                continue
            elif af.iv.chrom != ar.iv.chrom:
                counts["_ambiguous_readpair_position"] += 1
                continue
            else:
                rs = map_read_pair(af, ar)
                counts = update_count_vector(counts, rs)
                num_reads += 1
            if num_reads % 100000 == 0:
                sys.stderr.write("%d reads processed.\n" % num_reads)

    else:
        processed_chromosomes = dict()
        num_reads = 0
        current_chromosome = ""
        current_position = ""
        for a in reader(sam_file):
            if not a.aligned:
                continue
            if a.optional_field("NH") > 1:
                continue
            if current_chromosome != a.iv.chrom:
                if current_chromosome in processed_chromosomes:
                    raise SystemError(
                        "A chromosome that had finished to be processed before was found again in the alignment file, is your alignment file properly sorted by position?"
                    )
                processed_chromosomes[current_chromosome] = 1
                alignments = clean_read_queue(alignments, current_position)
                del alignments
                alignments = dict()
            if current_chromosome == a.iv.chrom and a.iv.start < current_position:
                raise SystemError(
                    "Current read position is smaller than previous reads, is your alignment file properly sorted by position?"
                )
            current_chromosome = a.iv.chrom
            current_position = a.iv.start
            if a.read.name and a.mate_aligned:
                if a.read.name in alignments:
                    b = alignments[a.read.name]
                    if a.pe_which == "first" and b.pe_which == "second":
                        af = a
                        ar = b
                    else:
                        af = b
                        ar = a
                    rs = map_read_pair(af, ar)
                    del alignments[a.read.name]
                    counts = update_count_vector(counts, rs)
                else:
                    if a.mate_start.chrom != a.iv.chrom:
                        counts["_ambiguous_readpair_position"] += 1
                        continue
                    else:
                        alignments[a.read.name] = a
            else:
                continue
            num_reads += 1
            if num_reads % 200000 == 0:
                alignments = clean_read_queue(alignments, current_position)
                sys.stderr.write("%d reads processed.\n" % (num_reads / 2))


# Step 3: Write out the results

fout = open(out_file, "w")
for fn in sorted(counts.keys()):
    fout.write("%s\t%d\n" % (fn, counts[fn]))
fout.close()
