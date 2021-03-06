import logging
from typing import Dict

import hail as hl

from gnomad.utils.filtering import filter_low_conf_regions
from gnomad.utils.vep import (
    filter_vep_to_canonical_transcripts,
    get_most_severe_consequence_for_summary,
)


logging.basicConfig(format="%(levelname)s (%(name)s %(lineno)s): %(message)s")
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)


def freq_bin_expr(
    freq_expr: hl.expr.ArrayExpression, index: int = 0
) -> hl.expr.StringExpression:
    """
	Returns case statement adding frequency string annotations based on input AC or AF.

	:param freq_expr: Array of structs containing frequency information.
	:param index: Which index of freq_expr to use for annotation. Default is 0. 
		Assumes freq_expr was calculated with `annotate_freq`.
		Frequency index 0 from `annotate_freq` is frequency for all
		pops calculated on adj genotypes only.
	:return: StringExpression containing bin name based on input AC or AF.
	"""
    return (
        hl.case(missing_false=True)
        .when(freq_expr[index].AC == 0, "Not found")
        .when(freq_expr[index].AC == 1, "Singleton")
        .when(freq_expr[index].AC == 2, "Doubleton")
        .when(freq_expr[index].AC <= 5, "AC 3 - 5")
        .when(freq_expr[index].AF < 1e-4, "AC 6 - 0.01%")
        .when(freq_expr[index].AF < 1e-3, "0.01% - 0.1%")
        .when(freq_expr[index].AF < 1e-2, "0.1% - 1%")
        .when(freq_expr[index].AF < 1e-1, "1% - 10%")
        .when(freq_expr[index].AF > 0.95, ">95%")
        .default("10% - 95%")
    )


def get_summary_counts_dict(
    allele_expr: hl.expr.ArrayExpression,
    lof_expr: hl.expr.StringExpression,
    no_lof_flags_expr: hl.expr.BooleanExpression,
    prefix_str: str = "",
) -> Dict[str, hl.expr.Int64Expression]:
    """
	Returns dictionary containing containing counts of multiple variant categories.

	Categories are:
		- Number of variants
		- Number of indels
		- Number of SNVs
		- Number of LoF variants
		- Number of LoF variants that pass LOFTEE
		- Number of LoF variants that pass LOFTEE without any flgs
		- Number of LoF variants annotated as "other splice" (OS) by LOFTEE
		- Number of LoF variants that fail LOFTEE

	..warning:: 
		Assumes `allele_expr` contains only two variants (multi-allelics have been split).

	:param allele_expr: ArrayExpression containing alleles.
	:param lof_expr: StringExpression containing LOFTEE annotation.
	:param no_lof_flags_expr: BooleanExpression indicating whether LoF variant has any flags.
	:param prefix_str: Desired prefix string for category names. Default is empty str.
	:return: Dict of categories and counts per category.
	"""
    logger.warning("This function expects that multi-allelic variants have been split!")
    return {
        f"{prefix_str}num_variants": hl.agg.count(),
        f"{prefix_str}indels": hl.agg.count_where(
            hl.is_indel(allele_expr[0], allele_expr[1])
        ),
        f"{prefix_str}snps": hl.agg.count_where(
            hl.is_snp(allele_expr[0], allele_expr[1])
        ),
        f"{prefix_str}LOF": hl.agg.count_where(hl.is_defined(lof_expr)),
        f"{prefix_str}pass_loftee": hl.agg.count_where(lof_expr == "HC"),
        f"{prefix_str}pass_loftee_no_flag": hl.agg.count_where(
            (lof_expr == "HC") & (no_lof_flags_expr)
        ),
        f"{prefix_str}loftee_os": hl.agg.count_where(lof_expr == "OS"),
        f"{prefix_str}fail_loftee": hl.agg.count_where(lof_expr == "LC"),
    }


def get_summary_counts(
    ht: hl.Table,
    freq_field: str = "freq",
    filter_field: str = "filters",
    filter_decoy: bool = False,
) -> hl.Table:
    """
	Generates a struct with summary counts across variant categories.

	Summary counts:
		- Number of variants
		- Number of indels
		- Number of SNVs
		- Number of LoF variants
		- Number of LoF variants that pass LOFTEE (including with LoF flags)
		- Number of LoF variants that pass LOFTEE without LoF flags
		- Number of OS (other splice) variants annotated by LOFTEE
		- Number of LoF variants that fail LOFTEE filters

	Also annotates Table's globals with total variant counts.

	Before calculating summary counts, function:
		- Filters out low confidence regions
		- Filters to canonical transcripts
		- Uses the most severe consequence 

	Assumes that:
		- Input HT is annotated with VEP.
		- Multiallelic variants have been split and/or input HT contains bi-allelic variants only.

	:param ht: Input Table.
	:param freq_field: Name of field in HT containing frequency annotation (array of structs). Default is "freq".
	:param filter_field: Name of field in HT containing variant filter information. Default is "filters".
	:param filter_decoy: Whether to filter decoy regions. Default is False.
	:return: Table grouped by frequency bin and aggregated across summary count categories. 
	"""
    logger.info("Filtering to PASS variants in high confidence regions...")
    ht = ht.filter((hl.len(ht[filter_field]) == 0))
    ht = filter_low_conf_regions(ht, filter_decoy=filter_decoy)

    logger.info(
        "Filtering to canonical transcripts and getting VEP summary annotations..."
    )
    ht = filter_vep_to_canonical_transcripts(ht)
    ht = get_most_severe_consequence_for_summary(ht)

    logger.info("Annotating with frequency bin information...")
    ht = ht.annotate(freq_bin=freq_bin_expr(ht[freq_field]))

    logger.info("Annotating HT globals with total counts per variant category...")
    summary_counts = ht.aggregate(
        hl.struct(
            **get_summary_counts_dict(
                ht.alleles, ht.lof, ht.no_lof_flags, prefix_str="total_"
            )
        )
    )
    ht = ht.annotate_globals(summary_counts=summary_counts)
    return ht.group_by("freq_bin").aggregate(
        **get_summary_counts_dict(ht.alleles, ht.lof, ht.no_lof_flags)
    )
