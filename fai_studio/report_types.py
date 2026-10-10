"""The report types the app can produce, and what each one means for layout and workflow."""

AS9102 = "AS9102 FAI"                      # Forms 1-3, ADMI-branded layout
AS9102_CLEAN = "AS9102 FAI - Clean"        # Forms 1-3, plain black & white AS9102 Rev C layout
STANDARD = "Standard Report"               # one inspection report laid out like AS9102 Form 3 (clean style)
INSPECTION = "Inspection Report"           # dimensional inspection report (nominal / tol / LSL / USL columns)

REPORT_TYPES = [AS9102, AS9102_CLEAN, STANDARD, INSPECTION]

DESCRIPTIONS = {
    AS9102: "AS9102 Forms 1, 2 and 3 in the ADMI layout (company banner, shaded rows, colour-coded results).",
    AS9102_CLEAN: "AS9102 Forms 1, 2 and 3 in the plain AS9102 Rev C layout: black & white, standard numbered "
                  "fields only - for customers who want the standard forms.",
    STANDARD: "A single inspection report laid out like AS9102 Form 3 (char no., location, designator, "
              "requirement, results, accept/reject, tooling, NCR, comments) with a header and sign-off.",
    INSPECTION: "Dimensional inspection report with nominal, tolerance, LSL / USL, actual and status columns.",
}


def is_fai(report_type: str) -> bool:
    """Forms 1 and 2 (part-number and product accountability) apply."""
    return (report_type or "").startswith("AS9102")


def is_clean(report_type: str) -> bool:
    """Plain black & white AS9102-style layout."""
    return report_type in (AS9102_CLEAN, STANDARD)


def number_prefix(report_type: str) -> str:
    return "FAI" if is_fai(report_type) else "IR"
