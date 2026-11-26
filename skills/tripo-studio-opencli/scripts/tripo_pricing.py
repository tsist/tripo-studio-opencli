#!/usr/bin/env python3
"""
Exact Tripo Studio Web pricing estimator aligned with live frontend `b3` pricing map
and Pinia `config-store` free trial rules (verified against studio.tripo3d.com).
"""
from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Optional

# Exact `b3` / `Dt` pricing table observed in studio.tripo3d.com (`CXyzmgTt.js`)
# and studio.tripo3d.ai (`Qy9e8hGF.js` + `Ds7oivs-.js`, verified 2026-11-26)
LATEST_HD_VERSION = "v3.5-20261108"        # Tripo H3.5 Preview (live default on studio.tripo3d.ai)
CN_DEFAULT_HD_VERSION = "v3.1-20260211"    # Tripo H3.1 (live latest on studio.tripo3d.com)
LATEST_LP_VERSION = "Nexus-v2.0-20260801"  # Tripo Smart Mesh P2.0 (live latest on CN & Global)
LATEST_RIG_BIPED_VERSION = "v3.0-20260909" # Tripo Rigging v3.0 (live on Global & API)
CN_RIG_BIPED_VERSION = "v1.0-20240301"     # Current CN web biped rigging version
LATEST_RIG_ANIMAL_VERSION = "v2.5-20260210"

_HD_VERSION_ALIASES: dict[str, str] = {
    "latest": LATEST_HD_VERSION,
    "h3.5": LATEST_HD_VERSION,
    "v3.5": LATEST_HD_VERSION,
    "h3.1": "v3.1-20260211",
    "v3.1": "v3.1-20260211",
    "h3.0": "v3.0-20250812",
    "v3.0": "v3.0-20250812",
    "h2.5": "v2.5-20250123",
    "v2.5": "v2.5-20250123",
}

_LP_VERSION_ALIASES: dict[str, str] = {
    "latest": LATEST_LP_VERSION,
    "p2": LATEST_LP_VERSION,
    "p2.0": LATEST_LP_VERSION,
    "p1": "Nexus-v1.0-20260214",
    "p1.0": "Nexus-v1.0-20260214",
}

_RIG_VERSION_ALIASES: dict[str, str] = {
    "latest": LATEST_RIG_BIPED_VERSION,
    "v3.0": LATEST_RIG_BIPED_VERSION,
    "v2.5": LATEST_RIG_ANIMAL_VERSION,
    "v2.0": "v2.0-20250506",
    "v1.0": CN_RIG_BIPED_VERSION,
}


def default_hd_version(region: str = "global") -> str:
    """Return latest live HD model version for the requested Studio region."""
    return LATEST_HD_VERSION if region == "global" else CN_DEFAULT_HD_VERSION


def resolve_hd_version(version: Optional[str], region: str = "global") -> str:
    if not version:
        return default_hd_version(region)
    key = version.strip().lower()
    return _HD_VERSION_ALIASES.get(key, version.strip())


def resolve_lp_version(version: Optional[str]) -> str:
    if not version:
        return LATEST_LP_VERSION
    key = version.strip().lower()
    return _LP_VERSION_ALIASES.get(key, version.strip())


def default_rig_version(rig_type: str = "biped", region: str = "global") -> str:
    if rig_type == "animal":
        return LATEST_RIG_ANIMAL_VERSION
    return LATEST_RIG_BIPED_VERSION if region == "global" else CN_RIG_BIPED_VERSION


def resolve_rig_version(version: Optional[str], rig_type: str = "biped", region: str = "global") -> str:
    if not version:
        return default_rig_version(rig_type, region)
    key = version.strip().lower()
    if key == "latest":
        return LATEST_RIG_ANIMAL_VERSION if rig_type == "animal" else LATEST_RIG_BIPED_VERSION
    return _RIG_VERSION_ALIASES.get(key, version.strip())


def is_h35_version(version: str) -> bool:
    return version.strip().lower().startswith("v3.5")


PRICING_B3: dict[str, int] = {
    "Completion": 5,
    "Export": 5,
    "FLUX_1_pro": 10,
    "GenerateBase": 15,
    "GenerateBaseH35": 30,
    "GenerateBaseH35Original": 100,
    "GenerateBaseNexus": 35,
    "GenerateBaseNexusV2": 100,
    "GenerateGenerateParts": 30,
    "GenerateGeometryQualityDetailed": 15,
    "GenerateQuad": 5,
    "GenerateSmartPoly": 20,
    "GenerateTextureQualityDetailed": 10,
    "GenerateTextureQualityExtreme": 20,
    "GenerateWithTexture": 25,
    "HomeImageToModel": 40,
    "HomeTextToModel": 40,
    "MagicBrush": 5,
    "PBR": 5,
    "Retopology_Quad": 10,
    "Retopology_SmartPoly": 30,
    "Retopology_Triangle": 5,
    "Rigging": 20,
    "Segmentation": 40,
    "TextureGeneration": 10,
    "TextureQualityDetailed": 10,
    "TextureQualityExtreme": 20,
    "TextureStyle": 5,
    "Upscaler": 10,
    "UpscalerExtreme": 20,
    "UvEditGenerate": 20,
    "UvEditRetry": 10,
}

VALID_TEXTURE_QUALITY = {"standard", "detailed", "extreme"}
VALID_GEOMETRY_QUALITY = {"standard", "detailed"}


@dataclass
class CreditEstimate:
    action: str
    raw_credits: int
    trial_discount: int
    net_credits: int
    trial_used: Optional[str]
    breakdown: list[str]
    benefit_discount: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


def estimate_generate(
    *,
    tier: str = "hd",  # "hd" | "smartmesh"
    hd_version: str = LATEST_HD_VERSION,
    lp_version: str = LATEST_LP_VERSION,
    mode: str = "single",  # "single" | "multiview" | "batch" | "text"
    batch_count: int = 1,
    variations: int = 1,
    quad: bool = False,
    texture: bool = True,
    pbr: bool = False,
    texture_quality: str = "detailed",  # "standard" | "detailed" | "extreme"
    geometry_quality: str = "detailed",  # "standard" | "detailed"
    generate_parts: bool = False,
    smart_poly: bool = False,
    smart_mesh_p2_trial: int = 0,
    ultra_texture_trial: int = 0,
) -> CreditEstimate:
    """Estimate exact Studio generation credits matching frontend reactive formula."""
    hd_version = resolve_hd_version(hd_version, region="global")
    lp_version = resolve_lp_version(lp_version)
    if tier not in {"hd", "smartmesh"}:
        raise ValueError(f"Unsupported tier: {tier}")
    if mode not in {'single','multiview','batch','text'}:
        raise ValueError(f'Unsupported input mode: {mode}')
    if variations not in {1,2,4} or (tier=='hd' and variations!=1):
        raise ValueError('Variations must be 1/2/4 for Smart Mesh, and 1 for High Detail.')
    if variations!=1 and (mode=='batch' or '2.0' not in lp_version):
        raise ValueError('Multiple model variants are supported only by non-batch Smart Mesh P2.')
    if texture_quality not in VALID_TEXTURE_QUALITY:
        raise ValueError(f"Unsupported texture_quality: {texture_quality}")
    if geometry_quality not in VALID_GEOMETRY_QUALITY:
        raise ValueError(f"Unsupported geometry_quality: {geometry_quality}")
    if mode == "batch" and batch_count < 1:
        raise ValueError("batch_count must be >= 1")

    # P2 variants are multiple face-budget outputs of one generation. The live
    # frontend V formula charges a single P2 unit, not one unit per variant.
    count = batch_count if mode == 'batch' else 1
    breakdown: list[str] = []
    trial_used: Optional[str] = None
    trial_discount = 0
    total = 0

    if tier == "hd":
        is_h35 = is_h35_version(hd_version)
        if is_h35 and quad:
            raise ValueError(
                "H3.5 Preview (v3.5-20261108) only supports triangle topology; "
                "use --tier smartmesh --quad or --hd-version v3.1-20260211 for quad."
            )
        if is_h35:
            # Verified against live Ds7oivs-.js + Qy9e8hGF.js: H3.5 payable base is
            # GenerateBaseH35 (30; original list 100) regardless of texture toggle.
            unit = PRICING_B3["GenerateBaseH35"]
            breakdown.append(
                f"H3.5Base={unit}(orig={PRICING_B3['GenerateBaseH35Original']})"
            )
        else:
            unit = PRICING_B3["GenerateWithTexture"] if texture else PRICING_B3["GenerateBase"]
            breakdown.append(f"{'WithTexture' if texture else 'Base'}={unit}")
        if quad:
            unit += PRICING_B3["GenerateQuad"]
            breakdown.append(f"Quad={PRICING_B3['GenerateQuad']}")
        if generate_parts:
            unit += PRICING_B3["GenerateGenerateParts"]
            breakdown.append(f"Parts={PRICING_B3['GenerateGenerateParts']}")
        if smart_poly:
            unit += PRICING_B3["GenerateSmartPoly"]
            breakdown.append(f"SmartPoly={PRICING_B3['GenerateSmartPoly']}")
        if texture and texture_quality == "detailed":
            unit += PRICING_B3["GenerateTextureQualityDetailed"]
            breakdown.append(f"TexDetailed={PRICING_B3['GenerateTextureQualityDetailed']}")
        elif texture and texture_quality == "extreme":
            unit += PRICING_B3["GenerateTextureQualityExtreme"]
            breakdown.append(f"TexExtreme={PRICING_B3['GenerateTextureQualityExtreme']}")
        if texture and pbr:
            unit += PRICING_B3["PBR"]
            breakdown.append(f"PBR={PRICING_B3['PBR']}")

        total = unit * count
        if (hd_version.startswith("v3.1") or is_h35) and geometry_quality == "detailed":
            geo_add = PRICING_B3["GenerateGeometryQualityDetailed"] * count
            total += geo_add
            breakdown.append(f"{'H3.5' if is_h35 else 'v3.1'}GeoDetailed={geo_add}")

        if texture and texture_quality == "extreme" and ultra_texture_trial > 0 and not generate_parts:
            trial_discount = total // count
            trial_used = "ultra_texture_trial"
    else:
        is_v2 = "2.0" in lp_version
        unit = (
            PRICING_B3["GenerateBaseNexusV2"]
            if is_v2
            else PRICING_B3["GenerateBaseNexus"] + (PRICING_B3["GenerateQuad"] if quad else 0)
        )
        breakdown.append(f"{'P2.0' if is_v2 else 'P1.0'}={unit}")
        total = unit * count
        if is_v2 and smart_mesh_p2_trial > 0:
            trial_discount = unit
            trial_used = "smart_mesh_p2_trial"

    net = max(0, total - trial_discount)
    return CreditEstimate(
        action=f"generate_{tier}_{mode}",
        raw_credits=total,
        trial_discount=trial_discount,
        net_credits=net,
        trial_used=trial_used,
        breakdown=breakdown,
    )


def estimate_postprocess(
    operation: str,
    *,
    quad: bool = True,
    smart_poly: bool = False,
    texture_quality: str = "standard",
    use_style: bool = False,
    upscale_extreme: bool = False,
    is_uv_retry: bool = False,
    is_owner: bool = True,
    uv_generate_trial: int = 0,
    upscaler_trial: int = 0,
    brush_mode: bool = False,
    premium_texture_edit: bool = False,
) -> CreditEstimate:
    """Estimate credit cost for Studio post-processing or export operations."""
    breakdown: list[str] = []
    trial_used: Optional[str] = None
    trial_discount = 0
    raw = 0
    benefit_discount = 0

    if operation == "segmentation":
        raw = PRICING_B3["Segmentation"]
        breakdown.append(f"Segmentation={raw}")
    elif operation == "fill-parts":
        raw = 0
        breakdown.append('CurrentFillPartsUI=0 (verified oTYZ5lRn.js; subscription eligibility still required)')
    elif operation == "retopology":
        if quad:
            raw = PRICING_B3["Retopology_Quad"]
            breakdown.append(f"Retopology_Quad={raw}")
        else:
            raw = PRICING_B3["Retopology_Triangle"]
            breakdown.append(f"Retopology_Triangle={raw}")
        if smart_poly:
            # The live component adds P1.0 to the chosen base topology cost.
            raw += PRICING_B3["Retopology_SmartPoly"]
            breakdown.append(f"Retopology_SmartPolyAddOn={PRICING_B3['Retopology_SmartPoly']}")
    elif operation == "smart-uv":
        raw = PRICING_B3["UvEditRetry"] if is_uv_retry else PRICING_B3["UvEditGenerate"]
        breakdown.append(f"{'UvEditRetry' if is_uv_retry else 'UvEditGenerate'}={raw}")
        if not is_uv_retry and uv_generate_trial > 0:
            trial_discount = raw
            trial_used = "uv_generate_trial"
    elif operation == "texture":
        raw = PRICING_B3["TextureGeneration"]
        breakdown.append(f"TextureGeneration={raw}")
        if texture_quality == "detailed":
            raw += PRICING_B3["TextureQualityDetailed"]
            breakdown.append(f"TextureQualityDetailed={PRICING_B3['TextureQualityDetailed']}")
        elif texture_quality == "extreme":
            raw += PRICING_B3["TextureQualityExtreme"]
            breakdown.append(f"TextureQualityExtreme={PRICING_B3['TextureQualityExtreme']}")
        if use_style:
            raw += PRICING_B3["TextureStyle"]
            breakdown.append(f"TextureStyle={PRICING_B3['TextureStyle']}")
    elif operation == "texture-edit":
        raw = PRICING_B3['MagicBrush']
        breakdown.append(f'MagicBrushPreview={raw}')
        if premium_texture_edit:
            benefit_discount=raw
            breakdown.append(f'PremiumTextureEditBenefit={benefit_discount}')
    elif operation == "texture-pbr":
        raw = PRICING_B3["PBR"]
        breakdown.append(f"PBR={raw}")
    elif operation == "texture-upscale":
        raw = PRICING_B3["UpscalerExtreme"] if upscale_extreme else PRICING_B3["Upscaler"]
        breakdown.append(f"{'UpscalerExtreme' if upscale_extreme else 'Upscaler'}={raw}")
        if not upscale_extreme and upscaler_trial > 0:
            trial_discount = raw
            trial_used = "upscaler_trial"
    elif operation == "rigging":
        raw = PRICING_B3["Rigging"]
        breakdown.append(f"Rigging={raw}")
    elif operation == "export":
        raw = 0 if is_owner else PRICING_B3["Export"]
        breakdown.append(f"{'OwnerExport=0' if is_owner else 'PublicExport=5'}")
    else:
        raise ValueError(f"Unsupported post-process operation: {operation}")

    net = max(0, raw - trial_discount - benefit_discount)
    return CreditEstimate(
        action=f"postprocess_{operation}",
        raw_credits=raw,
        trial_discount=trial_discount,
        net_credits=net,
        trial_used=trial_used,
        breakdown=breakdown,
        benefit_discount=benefit_discount,
    )
