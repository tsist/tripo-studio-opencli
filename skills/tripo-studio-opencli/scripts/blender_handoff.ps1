param(
    [Parameter(Mandatory = $true)]
    [string]$InputFile,

    [Parameter(Mandatory = $true)]
    [string]$TaskDir,

    [string]$ProjectId = "",

    [ValidateSet("auto", "glb", "fbx")]
    [string]$Mode = "auto",

    [ValidateSet("+X", "-X", "+Y", "-Y")]
    [string]$FrontAxis = "+X",

    [string]$ModelTag = "tripo-studio-web",

    [switch]$SkipPreview
)

$ErrorActionPreference = "Stop"

$BlenderCtl = $env:TRIPO_BLENDERCTL
$BlenderExe = $env:TRIPO_BLENDER_EXE
if ([string]::IsNullOrWhiteSpace($BlenderCtl) -or [string]::IsNullOrWhiteSpace($BlenderExe)) {
    throw "Set TRIPO_BLENDERCTL and TRIPO_BLENDER_EXE to existing compatible executables; no runtime is bundled."
}
$FbxPyWorker = Join-Path $PSScriptRoot "fbx_import_headless.py"

if (!(Test-Path -LiteralPath $InputFile -PathType Leaf)) {
    throw "Input file not found: $InputFile"
}
if (!(Test-Path -LiteralPath $BlenderCtl -PathType Leaf)) {
    throw "blenderctl.ps1 not found: $BlenderCtl"
}
if (!(Test-Path -LiteralPath $BlenderExe -PathType Leaf)) {
    throw "Blender 5.2 binary not found: $BlenderExe"
}

$ResolvedInput = (Resolve-Path -LiteralPath $InputFile).Path
$Ext = [System.IO.Path]::GetExtension($ResolvedInput).ToLowerInvariant()
if ($Mode -eq "auto") {
    if ($Ext -eq ".glb" -or $Ext -eq ".gltf") {
        $Mode = "glb"
    } elseif ($Ext -eq ".fbx") {
        $Mode = "fbx"
    } else {
        throw "Cannot infer handoff mode from extension '$Ext'; pass -Mode glb|fbx explicitly."
    }
}

$InputSha = (Get-FileHash -LiteralPath $ResolvedInput -Algorithm SHA256).Hash.ToLowerInvariant()
$Stem = [System.IO.Path]::GetFileNameWithoutExtension($ResolvedInput)
$HandoffDir = Join-Path $TaskDir "handoff"
$JobsDir = Join-Path $TaskDir "blenderctl_jobs"
$ExistingTargets = @(
    @($HandoffDir, $JobsDir) | Where-Object { Test-Path -LiteralPath $_ }
)
if ($ExistingTargets.Count -gt 0) {
    throw "Refusing to overwrite existing handoff or jobs target(s): $($ExistingTargets -join ', '). Use a new TaskDir."
}
New-Item -ItemType Directory -Force -Path $TaskDir | Out-Null
try {
    New-Item -ItemType Directory -Path $HandoffDir -ErrorAction Stop | Out-Null
}
catch {
    throw "Refusing to overwrite existing handoff target: $HandoffDir"
}
try {
    New-Item -ItemType Directory -Path $JobsDir -ErrorAction Stop | Out-Null
}
catch {
    throw "Refusing to overwrite existing jobs target: $JobsDir"
}
$ReportOutPath = Join-Path $HandoffDir "${Stem}_handoff_report.json"

# Compatibility facts are informational: no installation change and no folder-only conflict claim.
$DetectedTripoAddonFolders = @()
$BlenderUserScripts = Join-Path $env:APPDATA "Blender Foundation\Blender\5.2\scripts\addons"
if (Test-Path -LiteralPath $BlenderUserScripts) {
    Get-ChildItem -LiteralPath $BlenderUserScripts -ErrorAction SilentlyContinue | ForEach-Object {
        if ($_.Name -match "tripo") {
            $DetectedTripoAddonFolders += $_.FullName
        }
    }
}
$ConflictWarnings = @()
$BridgeCompatibility = @{
    official_studio_bridge = @{
        bind = "127.0.0.1:60600"
        coexistence_with_mcp_verified = $true
    }
    mcp = @{
        bind = "127.0.0.1:9876"
        coexistence_with_official_bridge_verified = $true
    }
    verified_observation = "Official Studio Bridge 1.1.0 and MCP were verified in the same Blender 5.2.1 process; 60600 and 9876 are distinct binds."
    folder_presence_implies_port_conflict = $false
    installation_modified = $false
}

$Result = @{
    ok               = $false
    mode             = $Mode
    input_file       = $ResolvedInput
    input_sha256     = $InputSha
    project_id       = $ProjectId
    front_axis       = $FrontAxis
    orientation_transform_applied = $false
    orientation_semantics = "declared_source_front_for_preview"
    conflict_warnings = $ConflictWarnings
    detected_tripo_named_addon_folders = $DetectedTripoAddonFolders
    bridge_compatibility = $BridgeCompatibility
}

if ($Mode -eq "glb") {
    $ParamsPath = Join-Path $HandoffDir "${Stem}_import_local.params.json"
    $ParamsObj = @{
        manifest = @{
            schema_version = "1.0"
            operation      = "import_local"
            input          = @{
                file            = $ResolvedInput
                expected_sha256 = $InputSha
            }
            provenance     = @{
                provider = "tripo-cn"
                task_id  = $ProjectId
                model    = $ModelTag
            }
            preview        = @{
                enabled    = (-not [bool]$SkipPreview)
                front_axis = $FrontAxis
                engine     = "CYCLES"
                width      = 1024
                height     = 1024
                samples    = 32
                views      = @("front", "threequarter", "side")
            }
        }
    }
    $ParamsObj | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $ParamsPath -Encoding utf8NoBOM

    $RawOut = & pwsh -NoProfile -File $BlenderCtl --jobs-dir $JobsDir modeling run --params $ParamsPath 2>&1
    $ExitCode = $LASTEXITCODE
    $Result["blenderctl_exit"] = $ExitCode
    $Result["blenderctl_raw"] = ($RawOut | Out-String).Trim()
    if ($ExitCode -ne 0) {
        $Result | ConvertTo-Json -Depth 8
        exit $ExitCode
    }
    $JsonOut = $RawOut | Out-String | ConvertFrom-Json
    $Result["modeling_job"] = $JsonOut
    if (-not $JsonOut.ok) {
        $Result | ConvertTo-Json -Depth 8
        exit 1
    }

    $CandidateBlend = $null
    if ($JsonOut.data -and $JsonOut.data.candidate) {
        $CandidateBlend = $JsonOut.data.candidate
    } elseif ($JsonOut.data -and $JsonOut.data.candidate_blend) {
        $CandidateBlend = $JsonOut.data.candidate_blend
    } elseif ($JsonOut.artifacts -and $JsonOut.artifacts.candidate) {
        $CandidateBlend = $JsonOut.artifacts.candidate
    } elseif ($JsonOut.artifacts -is [System.Array]) {
        foreach ($art in $JsonOut.artifacts) {
            if ($art.path -and $art.path.EndsWith(".blend")) {
                $CandidateBlend = $art.path
                break
            }
        }
    }
    if (-not $CandidateBlend -or -not (Test-Path -LiteralPath $CandidateBlend -PathType Leaf)) {
        $Result["ok"] = $false
        $Result["candidate_blend"] = $CandidateBlend
        $Result["error"] = "GLB modeling job reported success without an existing candidate_blend; inspect was not run."
        $Result["report_path"] = $ReportOutPath
        $Result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportOutPath -Encoding utf8NoBOM
        exit 2
    }
    $BlendSha = (Get-FileHash -LiteralPath $CandidateBlend -Algorithm SHA256).Hash.ToLowerInvariant()
    $InspectRaw = & pwsh -NoProfile -File $BlenderCtl --jobs-dir $JobsDir inspect $CandidateBlend --expected-sha256 $BlendSha 2>&1
    $InspectExit = $LASTEXITCODE
    $Result["candidate_blend"] = $CandidateBlend
    $Result["candidate_blend_sha256"] = $BlendSha
    $Result["inspect_exit"] = $InspectExit
    $Result["inspect"] = ($InspectRaw | Out-String) | ConvertFrom-Json
    if ($InspectExit -ne 0 -or (-not $Result["inspect"].ok)) {
        $Result["ok"] = $false
        $Result["error"] = "GLB candidate inspect failed."
        $Result["report_path"] = $ReportOutPath
        $Result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportOutPath -Encoding utf8NoBOM
        exit 1
    }
    $Result["ok"] = $true
}
else {
    if (!(Test-Path -LiteralPath $FbxPyWorker -PathType Leaf)) {
        throw "FBX headless worker missing: $FbxPyWorker"
    }
    $OutBlend = Join-Path $HandoffDir "${Stem}_fbx.blend"
    $FbxReportJson = Join-Path $HandoffDir "${Stem}_fbx_report.json"

    $BlenderArgs = @(
        "--background",
        "--factory-startup",
        "--disable-autoexec",
        "--offline-mode",
        "--python-exit-code", "23",
        "--python", $FbxPyWorker,
        "--",
        $ResolvedInput,
        $OutBlend,
        $FbxReportJson,
        $FrontAxis,
        $InputSha,
        $ProjectId
    )
    $BlenderLog = & $BlenderExe @BlenderArgs 2>&1
    $BlenderExit = $LASTEXITCODE
    $Result["blender_exit"] = $BlenderExit
    $Result["blender_log_tail"] = (($BlenderLog | Out-String).Split("`n") | Select-Object -Last 25) -join "`n"
    if ($BlenderExit -ne 0) {
        $Result | ConvertTo-Json -Depth 8
        exit $BlenderExit
    }
    if (!(Test-Path -LiteralPath $OutBlend -PathType Leaf) -or !(Test-Path -LiteralPath $FbxReportJson -PathType Leaf)) {
        throw "Headless FBX import completed without expected outputs: $OutBlend / $FbxReportJson"
    }

    $BlendSha = (Get-FileHash -LiteralPath $OutBlend -Algorithm SHA256).Hash.ToLowerInvariant()
    $InspectRaw = & pwsh -NoProfile -File $BlenderCtl --jobs-dir $JobsDir inspect $OutBlend --expected-sha256 $BlendSha 2>&1
    $InspectExit = $LASTEXITCODE
    if ($InspectExit -ne 0) {
        $Result["inspect_exit"] = $InspectExit
        $Result["inspect_raw"] = ($InspectRaw | Out-String)
        $Result | ConvertTo-Json -Depth 8
        exit $InspectExit
    }

    $Result["candidate_blend"] = $OutBlend
    $Result["candidate_blend_sha256"] = $BlendSha
    $Result["fbx_import_report"] = Get-Content -LiteralPath $FbxReportJson -Raw -Encoding utf8 | ConvertFrom-Json
    $Result["inspect_exit"] = $InspectExit
    $Result["inspect"] = ($InspectRaw | Out-String) | ConvertFrom-Json
    $Result["ok"] = [bool]($Result["inspect"].ok)
}

$Result["report_path"] = $ReportOutPath
$Result | ConvertTo-Json -Depth 12 | Set-Content -LiteralPath $ReportOutPath -Encoding utf8NoBOM
$Result | ConvertTo-Json -Depth 12
