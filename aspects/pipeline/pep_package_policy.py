"""Shared package-matching rules for the PEP component/package policy (pep-capture-policy/1).

This is the ONE place that decides whether a physical package is the one a component certifies for a
given package family, PostgreSQL major and release version. Request validation
(aspects/pipeline/pep_request.py), cert-plan target selection (pipeline/pep_cert_plan.py), the
invocation planner's backstop (pipeline/pep_invocation_plan.py) and the per-run component tests all
call it, so their rules cannot diverge.

A policy entry has one of two forms:
  * flat: ``allowed_runtime_package_names`` -- ordered alternative names of the one runtime package,
    the same for every family and PG major (rag, pgedge-capture-smoke). An optional
    ``uncertified_runtime_package_names`` (with a required ``uncertified_reason``) names the OTHER
    runtime packages the release builds in each cell but the component does not certify (e.g. a CLI
    or an arch-independent web UI beside the server it tests); they are reported, never silently
    dropped. Such an entry accounts for every runtime package it expects, so any OTHER name is
    ``package_unclassified`` (a coverage gap downstream), never the silent exclusion. A flat entry
    without the list is unchanged;
  * PG-coupled templates: ``"pg_coupled": true`` with ``runtime_package_templates``, a per-family
    list of name templates. ``{pg}`` is the PostgreSQL major the package was built for and
    ``{series}`` is the product series, derived from the release version's leading major through
    ``series_by_version_major``. Example (spock): rpm ``pgedge-spock{series}_{pg}``, deb
    ``pgedge-postgresql-{pg}-spock{series}`` and ``{"5": "50", "6": "60"}``, so release 5.0.13
    built for PG17 expects ``pgedge-spock50_17`` (rpm) or ``pgedge-postgresql-17-spock50`` (deb).
    An optional ``uncertified_runtime_package_templates`` (same shape) names runtime companion
    packages the component builds but deliberately does not certify, with a required
    ``uncertified_reason``; they are reported, never silently dropped.

``classify`` returns None for the package to certify, or ONE reason code. ``package_not_allowed``
is the existing, intentional policy exclusion (an unrelated package). Every other reason means
"this is the component's package, but for the wrong family / PG major / series", a known runtime
companion that is not certified, or (a flat entry that names its companions) a package the entry
does not account for; consumers keep those visible. Stdlib only, pure.
"""
from __future__ import annotations

import re

FAMILIES = ("rpm", "deb")

R_NOT_ALLOWED = "package_not_allowed"            # unrelated package (existing policy exclusion)
R_FAMILY = "package_family_mismatch"             # the component's package, for the other family
R_PG = "pg_major_mismatch"                       # ... built for another PostgreSQL major
R_SERIES = "series_mismatch"                     # ... of another product series
R_NO_PG = "pg_major_unknown"                     # PG-coupled policy, but no canonical PG major known
R_NO_SERIES = "series_unknown"                   # the release version maps to no known series
R_UNCERTIFIED = "runtime_not_certified"          # a declared runtime companion, not certified
R_UNCLASSIFIED = "package_unclassified"          # flat entry naming its companions: a name it does not list

COUPLED_KEYS = ("pg_coupled", "runtime_package_templates", "series_by_version_major",
                "uncertified_runtime_package_templates", "uncertified_reason")
# A flat entry's optional uncertified companions (``uncertified_reason`` is shared with the coupled form).
FLAT_UNCERTIFIED_KEYS = ("uncertified_runtime_package_names", "uncertified_reason")

_PLACEHOLDER_RE = re.compile(r"\{([a-z]+)\}")
_PLACEHOLDERS = {"pg": r"(?P<pg>[0-9]+)", "series": r"(?P<series>[0-9]+)"}
_PG_MAJOR_RE = re.compile(r"[1-9][0-9]*")
_SERIES_RE = re.compile(r"[0-9]+")
_VERSION_MAJOR_RE = re.compile(r"([0-9]+)\.")


def _nonblank(x):
    return isinstance(x, str) and x.strip() != "" and x == x.strip()


def is_coupled(entry):
    """True for a PG-coupled template entry (``pg_coupled`` is strictly True)."""
    return isinstance(entry, dict) and entry.get("pg_coupled") is True


def _template_errors(label, tmpl, uses_series):
    if not _nonblank(tmpl):
        return ["%s must be a nonblank string without surrounding whitespace" % label]
    names = _PLACEHOLDER_RE.findall(tmpl)
    stripped = _PLACEHOLDER_RE.sub("", tmpl)
    errs = []
    if "{" in stripped or "}" in stripped:
        errs.append("%s has a malformed or unknown placeholder: %r" % (label, tmpl))
    elif any(n not in _PLACEHOLDERS for n in names):
        errs.append("%s has an unknown placeholder: %r" % (label, tmpl))
    elif names.count("pg") != 1:
        errs.append("%s must contain {pg} exactly once: %r" % (label, tmpl))
    elif names.count("series") > 1:
        errs.append("%s may contain {series} at most once: %r" % (label, tmpl))
    if "series" in names:
        uses_series.append(tmpl)
    return errs


def _family_templates_errors(label, value, uses_series):
    if not isinstance(value, dict) or not value:
        return ["%s must be a non-empty object keyed by package family" % label]
    errs = []
    for fam, tmpls in value.items():
        if fam not in FAMILIES:
            errs.append("%s has an unknown package family %r" % (label, fam))
            continue
        if not (isinstance(tmpls, list) and tmpls):
            errs.append("%s.%s must be a non-empty list" % (label, fam))
            continue
        if len(set(t for t in tmpls if isinstance(t, str))) != len(tmpls):
            errs.append("%s.%s has duplicate templates" % (label, fam))
        for i, t in enumerate(tmpls):
            errs.extend(_template_errors("%s.%s[%d]" % (label, fam, i), t, uses_series))
    return errs


def _flat_uncertified_errors(entry, certified):
    unc = entry.get("uncertified_runtime_package_names")
    errs = []
    if not (isinstance(unc, list) and unc and all(_nonblank(x) for x in unc)):
        errs.append("uncertified_runtime_package_names must be a non-empty list of nonblank strings")
    elif len(set(unc)) != len(unc):
        errs.append("uncertified_runtime_package_names has duplicate package names")
    elif set(unc) & set(certified):
        errs.append("uncertified_runtime_package_names names a package that is also certified: %s"
                    % sorted(set(unc) & set(certified)))
    if not _nonblank(entry.get("uncertified_reason")):
        errs.append("uncertified_reason must be a nonblank string")
    return errs


def flat_uncertified_names(entry):
    """A flat entry's named uncertified runtime packages (string entries only), else ()."""
    if is_coupled(entry) or not isinstance(entry, dict):
        return ()
    unc = entry.get("uncertified_runtime_package_names")
    return tuple(x for x in unc if isinstance(x, str)) if isinstance(unc, list) else ()


def validate_entry(entry):
    """Deterministic list of shape errors for one policy entry (empty == valid). Both loaders
    (request and capture) and the cert-plan envelope use it, so they accept exactly the same
    entries. A flat entry without uncertified names is validated exactly as before."""
    if not isinstance(entry, dict):
        return ["entry is not an object"]
    errs = []
    ebv = entry.get("expected_binary_version")
    if not (ebv is None or isinstance(ebv, str)):
        errs.append("expected_binary_version must be a string")
    flat = entry.get("allowed_runtime_package_names")
    coupled_keys = [k for k in COUPLED_KEYS if k in entry]
    flat_unc = "uncertified_runtime_package_names" in entry
    if flat is not None or flat_unc or not coupled_keys:
        if flat is None and flat_unc and [k for k in coupled_keys if k != "uncertified_reason"]:
            return errs + ["uncertified_runtime_package_names is only for flat entries (a PG-coupled "
                           "entry names companions in uncertified_runtime_package_templates)"]
        # a flat entry may carry uncertified_reason only together with its uncertified names
        mixed = [k for k in coupled_keys if not (k == "uncertified_reason" and flat_unc)]
        if mixed == ["uncertified_reason"]:
            return errs + ["uncertified_reason needs uncertified_runtime_package_names on a flat entry"]
        if mixed:
            return errs + ["allowed_runtime_package_names cannot be combined with PG-coupled keys %s"
                           % mixed]
        if not (isinstance(flat, list) and flat and all(_nonblank(x) for x in flat)):
            return errs + ["needs a nonempty allowed_runtime_package_names list of nonblank strings"]
        if len(set(flat)) != len(flat):
            errs.append("allowed_runtime_package_names has duplicate package names")
        if flat_unc:
            errs.extend(_flat_uncertified_errors(entry, flat))
        return errs
    if entry.get("pg_coupled") is not True:
        return errs + ["PG-coupled templates need \"pg_coupled\": true"]
    uses_series = []
    errs.extend(_family_templates_errors("runtime_package_templates",
                                         entry.get("runtime_package_templates"), uses_series))
    if "uncertified_runtime_package_templates" in entry or "uncertified_reason" in entry:
        errs.extend(_family_templates_errors("uncertified_runtime_package_templates",
                                             entry.get("uncertified_runtime_package_templates"), uses_series))
        if not _nonblank(entry.get("uncertified_reason")):
            errs.append("uncertified_reason must be a nonblank string")
    smap = entry.get("series_by_version_major")
    if smap is not None:
        if not (isinstance(smap, dict) and smap
                and all(isinstance(k, str) and _PG_MAJOR_RE.fullmatch(k) for k in smap)
                and all(isinstance(v, str) and _SERIES_RE.fullmatch(v) for v in smap.values())):
            errs.append("series_by_version_major must map version majors to series digits, "
                        "e.g. {\"5\": \"50\"}")
        elif len(set(smap.values())) != len(smap):
            errs.append("series_by_version_major maps two version majors to one series")
    if uses_series and smap is None:
        errs.append("templates use {series} but series_by_version_major is missing")
    if smap is not None and not uses_series:
        errs.append("series_by_version_major is set but no template uses {series}")
    return errs


def series_for_version(entry, version):
    """The product series for a release version (``"5.0.13"`` -> ``"50"``), or None when the entry
    has no series map, the version has no leading ``<major>.``, or the major is not mapped."""
    smap = entry.get("series_by_version_major") if isinstance(entry, dict) else None
    if not isinstance(smap, dict) or not isinstance(version, str):
        return None
    m = _VERSION_MAJOR_RE.match(version)
    return smap.get(m.group(1)) if m else None


def _expand(tmpl, pg, series):
    return tmpl.replace("{pg}", pg).replace("{series}", series if series is not None else "{series}")


def _canonical_pg(pg_major):
    pg = str(pg_major) if isinstance(pg_major, (str, int)) and not isinstance(pg_major, bool) else None
    return pg if pg is not None and _PG_MAJOR_RE.fullmatch(pg) else None


def expected_names(entry, family, pg_major, version):
    """The package name(s) this entry certifies for one family, PG major and release version, in
    policy order (canonical first). A flat entry returns its names unchanged (family- and
    PG-independent, as before). A coupled entry returns () when the family has no templates, the
    PG major is not canonical, or a {series} template has no series for the version."""
    if not is_coupled(entry):
        flat = entry.get("allowed_runtime_package_names") if isinstance(entry, dict) else None
        return tuple(x for x in flat if isinstance(x, str)) if isinstance(flat, list) else ()
    pg = _canonical_pg(pg_major)
    tmpls = (entry.get("runtime_package_templates") or {}).get(family) or []
    series = series_for_version(entry, version)
    if pg is None:
        return ()
    return tuple(_expand(t, pg, series) for t in tmpls
                 if isinstance(t, str) and ("{series}" not in t or series is not None))


def _parse(entry, name):
    """Every (kind, family, pg, series) a name matches across the entry's templates."""
    out = []
    for kind, key in (("runtime", "runtime_package_templates"),
                      ("uncertified", "uncertified_runtime_package_templates")):
        for fam, tmpls in (entry.get(key) or {}).items():
            for t in tmpls if isinstance(tmpls, list) else []:
                if not isinstance(t, str):
                    continue
                pattern = "".join(_PLACEHOLDERS.get(part, re.escape(part)) if i % 2 else re.escape(part)
                                  for i, part in enumerate(_PLACEHOLDER_RE.split(t)))
                m = re.fullmatch(pattern, name)
                if m:
                    out.append((kind, fam, m.groupdict().get("pg"), m.groupdict().get("series")))
    return out


def classify(entry, name, family, pg_major, version):
    """None if ``name`` is the package this entry certifies for (family, PG major, release
    version); otherwise one reason code (module docstring). A coupled entry checks family first,
    then PG major, then series, so the reason names the most basic mismatch."""
    if not isinstance(name, str):
        return R_NOT_ALLOWED
    if not is_coupled(entry):
        if name in expected_names(entry, family, pg_major, version):
            return None
        companions = flat_uncertified_names(entry)
        if name in companions:
            return R_UNCERTIFIED
        return R_UNCLASSIFIED if companions else R_NOT_ALLOWED
    if name in expected_names(entry, family, pg_major, version):
        return None
    matches = _parse(entry, name)
    if not matches:
        return R_NOT_ALLOWED
    pg = _canonical_pg(pg_major)
    series = series_for_version(entry, version)
    same_family = [m for m in matches if m[1] == family]
    if not same_family:
        return R_FAMILY
    if pg is None:
        return R_NO_PG
    same_pg = [m for m in same_family if m[2] == pg]
    if not same_pg:
        return R_PG
    if any(m[3] is not None for m in same_pg) and series is None:
        return R_NO_SERIES
    exact = [m for m in same_pg if m[3] is None or m[3] == series]
    if not exact:
        return R_SERIES
    if any(m[0] == "uncertified" for m in exact):
        return R_UNCERTIFIED
    return R_NOT_ALLOWED      # defensive: a runtime template match is returned by expected_names


def is_component_runtime_package(entry, name):
    """True if ``name`` is one of this component's certifiable runtime packages for ANY family,
    PG major or series (a coupled entry), or one of its flat names. Lets a test confirm that the
    requested package is the only package of the component installed."""
    if not isinstance(name, str):
        return False
    if not is_coupled(entry):
        return name in expected_names(entry, None, None, None)
    return any(kind == "runtime" for kind, _fam, _pg, _series in _parse(entry, name))


def describe(entry):
    """The policy facts a cert-plan records for traceability. A flat entry adds only its uncertified
    names and reason, when it has them (otherwise None: plain flat entries keep today's shape)."""
    if not is_coupled(entry):
        if not isinstance(entry, dict) or "uncertified_runtime_package_names" not in entry:
            return None
        return {"uncertified_runtime_package_names": entry.get("uncertified_runtime_package_names"),
                "uncertified_reason": entry.get("uncertified_reason")}
    out = {"pg_coupled": True,
           "runtime_package_templates": entry.get("runtime_package_templates"),
           "series_by_version_major": entry.get("series_by_version_major")}
    if "uncertified_runtime_package_templates" in entry:
        out["uncertified_runtime_package_templates"] = entry.get("uncertified_runtime_package_templates")
        out["uncertified_reason"] = entry.get("uncertified_reason")
    return out
