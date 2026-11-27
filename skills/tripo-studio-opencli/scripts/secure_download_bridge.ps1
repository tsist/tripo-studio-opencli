[CmdletBinding()]
param(
    [ValidateSet("Doctor", "Download", "Status")]
    [string]$Action = "Doctor",

    [string]$JobId,
    [string]$StateRoot
)

# Small security wrapper around the installed smart-download skill.
# It deliberately has no -Uri parameter: Download reads a JSON object from
# stdin so signed URLs never appear in an OS command line.

$ErrorActionPreference = "Stop"
[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)

$EngineScript = $env:TRIPO_SMART_DOWNLOAD_ENGINE
if ([string]::IsNullOrWhiteSpace($EngineScript)) { throw "Set TRIPO_SMART_DOWNLOAD_ENGINE to an existing compatible smart-download.ps1; nothing is installed automatically." }
$MaxPayloadBytes = 262144
$urlForRedaction = ""

function Protect-LogText {
    param([AllowEmptyString()][string]$Text)
    if ($null -eq $Text) { return "" }
    return [regex]::Replace($Text, '(?i)https?://[^\s<>'']+', {
        param($match)
        try {
            $uri = [Uri]$match.Value
            return $uri.Scheme + '://' + $uri.Host + '/[redacted-url]'
        }
        catch { return '[redacted-url]' }
    })
}

function Write-JsonResult {
    param([object]$Value)
    $json = $Value | ConvertTo-Json -Depth 8 -Compress
    [Console]::Out.WriteLine($json)
}

function Read-StdinJson {
    if (-not [Console]::IsInputRedirected) {
        throw "Download requires a JSON payload on redirected stdin."
    }
    $reader = $null
    $text = $null
    try {
        $stream = [Console]::OpenStandardInput()
        $reader = [IO.StreamReader]::new(
            $stream,
            [Text.UTF8Encoding]::new($false),
            $true
        )
        $text = $reader.ReadToEnd()
    }
    catch {
        throw "Could not read the stdin payload."
    }
    finally {
        if ($null -ne $reader) { $reader.Dispose() }
    }
    if ([string]::IsNullOrWhiteSpace($text)) {
        throw "Download requires a JSON payload on stdin."
    }
    if ($text.Length -gt $MaxPayloadBytes) {
        throw "The stdin payload is too large."
    }
    try {
        $payload = $text | ConvertFrom-Json
    }
    catch {
        throw "The stdin payload is not valid JSON."
    }
    if ($null -eq $payload -or $payload -is [System.Array] -or $payload -is [string]) {
        throw "The stdin payload must be a JSON object."
    }
    return $payload
}

function Get-PayloadValue {
    param(
        [object]$Payload,
        [string]$Name
    )
    $property = $Payload.PSObject.Properties[$Name]
    if ($null -eq $property) { return $null }
    return $property.Value
}

function Assert-AbsolutePath {
    param(
        [string]$Path,
        [string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Path)) {
        throw "$Label is required."
    }
    if ($Path -match '[\x00-\x1f\x7f]') {
        throw "$Label contains control characters."
    }
    if ($Path.StartsWith('\\?\') -or $Path.StartsWith('\\.\')) {
        throw "$Label must not use a Windows device path."
    }
    if (-not ($Path -match '^(?:[A-Za-z]:[\\/]|\\\\[^\\/]+[\\/][^\\/]+)')) {
        throw "$Label must be an absolute path."
    }
    $full = [IO.Path]::GetFullPath($Path)
    $parts = $Path -split '[\\/]'
    if ($parts -contains '..' -or $parts -contains '.') {
        throw "$Label must not contain '.' or '..' components."
    }
    $tail = if ($Path -match '^[A-Za-z]:') { $Path.Substring(2) } else { $Path }
    if ($tail.Contains(':')) {
        throw "$Label must not contain a Windows ADS colon."
    }
    return $full
}

function Assert-SafeLeaf {
    param(
        [string]$Name,
        [string]$Label
    )
    if ([string]::IsNullOrWhiteSpace($Name)) {
        throw "$Label must include a name."
    }
    if ($Name -ne $Name.Trim()) {
        throw "$Label must not have leading/trailing whitespace."
    }
    if ($Name -match '[\x00-\x1f\x7f]') {
        throw "$Label contains control characters."
    }
    if ($Name -in @('.', '..') -or $Name.Contains('..')) {
        throw "$Label must not contain '..'."
    }
    if ($Name -match '[<>:"/\\|?*]') {
        throw "$Label must not contain Windows path separators or forbidden filename characters."
    }
    if ($Name.EndsWith('.') -or $Name.EndsWith(' ')) {
        throw "$Label must not end with a dot or space."
    }
    $stem = ($Name -split '\.', 2)[0].ToUpperInvariant()
    if ($stem -match '^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])$') {
        throw "$Label is a reserved Windows device name."
    }
}

function Get-StateRoot {
    param([string]$Candidate)
    if ([string]::IsNullOrWhiteSpace($Candidate)) {
        $Candidate = Join-Path (Get-Location) '.codex-downloads'
    }
    $full = Assert-AbsolutePath $Candidate 'state_root'
    Assert-SafeLeaf ([IO.Path]::GetFileName($full)) 'state_root name'
    return $full
}

function Invoke-Engine {
    param([hashtable]$Parameters)
    if (-not (Test-Path -LiteralPath $EngineScript -PathType Leaf)) {
        throw "The smart-download engine is missing."
    }

    $engineOutput = & $EngineScript @Parameters 2>&1
    $text = @(
        $engineOutput | ForEach-Object {
            if ($_ -is [System.Management.Automation.ErrorRecord]) {
                $_.ToString()
            }
            else {
                [string]$_
            }
        }
    ) -join "`n"

    $start = $text.IndexOf('{')
    $end = $text.LastIndexOf('}')
    if ($start -lt 0 -or $end -lt $start) {
        throw "The download engine returned no JSON object."
    }
    try {
        return $text.Substring($start, $end - $start + 1) | ConvertFrom-Json
    }
    catch {
        throw "The download engine returned malformed JSON."
    }
}

function Protect-Result {
    param([object]$Result)
    if ($null -eq $Result) {
        throw "The download engine returned an empty result."
    }
    foreach ($name in @('error', 'message', 'note')) {
        $property = $Result.PSObject.Properties[$name]
        if ($null -ne $property -and $null -ne $property.Value) {
            $property.Value = Protect-LogText ([string]$property.Value)
        }
    }
    $tailProperty = $Result.PSObject.Properties['log_tail']
    if ($null -ne $tailProperty -and $null -ne $tailProperty.Value) {
        $tailProperty.Value = @(
            $tailProperty.Value | ForEach-Object { Protect-LogText ([string]$_) }
        )
    }
    return $Result
}

try {
    switch ($Action) {
        "Doctor" {
            $root = Get-StateRoot $StateRoot
            $doctor = Invoke-Engine @{
                Action = 'Doctor'
                StateRoot = $root
            }
            Write-JsonResult (Protect-Result $doctor)
        }

        "Status" {
            if ([string]::IsNullOrWhiteSpace($JobId)) {
                throw "Status requires -JobId."
            }
            if ($JobId -notmatch '^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$') {
                throw "Invalid job ID."
            }
            $root = Get-StateRoot $StateRoot
            $status = Invoke-Engine @{
                Action = 'Status'
                JobId = $JobId
                StateRoot = $root
            }
            Write-JsonResult (Protect-Result $status)
        }

        "Download" {
            $payload = Read-StdinJson

            $urlForRedaction = [string](Get-PayloadValue $payload 'url')
            if ([string]::IsNullOrWhiteSpace($urlForRedaction)) {
                throw "Download payload requires a URL."
            }
            if ($urlForRedaction -match '[\s\x00-\x1f\x7f]') {
                throw "URI must not contain whitespace or control characters."
            }
            $parsedUri = $null
            if (-not [Uri]::TryCreate($urlForRedaction, [UriKind]::Absolute, [ref]$parsedUri)) {
                throw "Invalid absolute URI."
            }
            if ($parsedUri.Scheme -notin @('http', 'https')) {
                throw "Only HTTP and HTTPS URLs are allowed."
            }

            $destinationValue = Get-PayloadValue $payload 'destination'
            if ($null -eq $destinationValue -or [string]::IsNullOrWhiteSpace([string]$destinationValue)) {
                throw "Download payload requires a destination."
            }
            $destination = Assert-AbsolutePath ([string]$destinationValue) 'destination'
            Assert-SafeLeaf ([IO.Path]::GetFileName($destination)) 'destination filename'
            if (Test-Path -LiteralPath $destination) {
                throw "Final destination already exists; refusing to overwrite it."
            }

            $stateValue = Get-PayloadValue $payload 'state_root'
            if ($null -eq $stateValue -or [string]::IsNullOrWhiteSpace([string]$stateValue)) {
                $stateValue = $StateRoot
            }
            $root = Get-StateRoot ([string]$stateValue)

            $expected = [long]0
            $expectedValue = Get-PayloadValue $payload 'expected_bytes'
            if ($null -ne $expectedValue) {
                $expectedText = [string]$expectedValue
                if ($expectedText -notmatch '^\d+$') {
                    throw "expected_bytes must be a positive integer."
                }
                $expected = [long]$expectedText
                if ($expected -le 0) {
                    throw "expected_bytes must be greater than zero when supplied."
                }
            }

            $sha = ""
            $shaValue = Get-PayloadValue $payload 'sha256'
            if ($null -ne $shaValue) {
                $sha = [string]$shaValue
                if ($sha -notmatch '^[A-Fa-f0-9]{64}$') {
                    throw "sha256 must contain exactly 64 hexadecimal characters."
                }
                $sha = $sha.ToLowerInvariant()
            }

            $connections = 4
            $connectionValue = Get-PayloadValue $payload 'connections'
            if ($null -ne $connectionValue) {
                $connectionText = [string]$connectionValue
                if ($connectionText -notmatch '^\d+$') {
                    throw "connections must be an integer."
                }
                $connections = [int]$connectionText
                if ($connections -lt 1 -or $connections -gt 16) {
                    throw "connections must be between 1 and 16."
                }
            }

            $engineParameters = @{
                Action = 'Download'
                Uri = $urlForRedaction
                Destination = $destination
                StateRoot = $root
                Connections = $connections
            }
            if ($expected -gt 0) {
                $engineParameters.ExpectedBytes = $expected
            }
            if ($sha) {
                $engineParameters.Sha256 = $sha
            }
            $backendValue = Get-PayloadValue $payload 'backend'
            if ($null -ne $backendValue) {
                if ($backendValue -isnot [string]) { throw "backend must match aria2 or curl." }
                switch -CaseSensitive ($backendValue) {
                    'aria2' { $engineParameters.Backend = 'Aria2' }
                    'curl' { $engineParameters.Backend = 'Curl' }
                    default { throw "backend must match aria2 or curl." }
                }
            }

            $job = Invoke-Engine $engineParameters
            Write-JsonResult (Protect-Result $job)
        }
    }
    exit 0
}
catch {
    $message = [string]$_.Exception.Message
    if ($urlForRedaction) {
        $message = $message.Replace($urlForRedaction, '[redacted-url]')
    }
    $message = Protect-LogText $message
    if ([string]::IsNullOrWhiteSpace($message)) {
        $message = "Download bridge failed."
    }
    Write-JsonResult ([ordered]@{
        ok = $false
        error = $message
    })
    exit 1
}
