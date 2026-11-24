#requires -Version 5.1
param(
    [Parameter(Position=0)][string]$Path,
    [Parameter(ValueFromRemainingArguments)][string[]]$Remaining = @()
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Read-Bytes([IO.BinaryReader]$Reader, [int]$Length) {
    $bytes = $Reader.ReadBytes($Length)
    if ($bytes.Length -ne $Length) { throw 'Truncated UnityFS data' }
    return ,$bytes
}

function Read-BigEndian([IO.BinaryReader]$Reader, [int]$Length) {
    $bytes = Read-Bytes $Reader $Length
    if ([BitConverter]::IsLittleEndian) { [Array]::Reverse($bytes) }
    switch ($Length) {
        2 { [BitConverter]::ToUInt16($bytes, 0) }
        4 { [BitConverter]::ToUInt32($bytes, 0) }
        8 { [BitConverter]::ToUInt64($bytes, 0) }
    }
}

function Read-CString([IO.BinaryReader]$Reader) {
    $bytes = [Collections.Generic.List[byte]]::new()
    while (($value = $Reader.ReadByte()) -ne 0) {
        if ($value -gt 127) { throw 'Invalid UnityFS header string' }
        $bytes.Add($value)
    }
    return [Text.Encoding]::ASCII.GetString($bytes.ToArray())
}

function Expand-UnityBlock([byte[]]$Data, [int]$Mode, [int]$Size) {
    if ($Mode -eq 0) {
        if ($Data.Length -ne $Size) { throw 'UnityFS block length mismatch' }
        return ,$Data
    }
    if ($Mode -ne 2 -and $Mode -ne 3) { throw 'Unsupported UnityFS compression method' }

    $output = [byte[]]::new($Size)
    $source = 0
    $destination = 0
    while ($source -lt $Data.Length) {
        $token = $Data[$source++]
        $literal = $token -shr 4
        if ($literal -eq 15) {
            do {
                if ($source -ge $Data.Length) { throw 'Truncated LZ4 literal length' }
                $extension = $Data[$source++]
                $literal += $extension
            } while ($extension -eq 255)
        }
        if ($literal -gt ($Data.Length - $source) -or $literal -gt ($Size - $destination)) {
            throw 'Invalid LZ4 literal length'
        }
        [Buffer]::BlockCopy($Data, $source, $output, $destination, $literal)
        $source += $literal
        $destination += $literal
        if ($source -eq $Data.Length) { break }

        if ($Data.Length - $source -lt 2) { throw 'Truncated LZ4 match offset' }
        $distance = [int]$Data[$source] + (256 * [int]$Data[($source + 1)])
        $source += 2
        if ($distance -eq 0 -or $distance -gt $destination) { throw 'Invalid LZ4 match offset' }
        $match = ($token -band 15) + 4
        if ($match -eq 19) {
            do {
                if ($source -ge $Data.Length) { throw 'Truncated LZ4 match length' }
                $extension = $Data[$source++]
                $match += $extension
            } while ($extension -eq 255)
        }
        if ($match -gt ($Size - $destination)) { throw 'Invalid LZ4 match length' }

        # Reuse bytes as they are decoded when the match overlaps its output.
        $reference = $destination - $distance
        $available = $distance
        while ($match -gt 0) {
            $length = [Math]::Min($match, $available)
            [Buffer]::BlockCopy($output, $reference, $output, $destination, $length)
            $destination += $length
            $available += $length
            $match -= $length
        }
    }
    if ($destination -ne $Size) { throw 'UnityFS block length mismatch' }
    return ,$output
}

function Get-UnityFingerprint([string]$BundlePath) {
    $stream = [IO.File]::OpenRead($BundlePath)
    $reader = [IO.BinaryReader]::new($stream)
    try {
        if ((Read-CString $reader) -cne 'UnityFS') { throw 'Not UnityFS' }
        $version = Read-BigEndian $reader 4
        if ($version -lt 6 -or $version -gt 9) { throw 'Unsupported UnityFS version' }
        $null = Read-CString $reader
        $revision = Read-CString $reader
        $size = Read-BigEndian $reader 8
        $packedInfo = Read-BigEndian $reader 4
        $plainInfo = Read-BigEndian $reader 4
        $flags = Read-BigEndian $reader 4
        if ($size -ne $stream.Length) { throw 'Bundle file size mismatch' }

        $headerEnd = $stream.Position
        if ($version -ge 7 -or $revision -match '^2019\.4\.(1[5-9]|[2-9]\d|\d{3,})') { $headerEnd = ($headerEnd + 15) -band (-bnot 15) }
        $infoAtEnd = ($flags -band 0x80) -ne 0
        $infoOffset = if ($infoAtEnd) { $stream.Length - $packedInfo } else { $headerEnd }
        if ($infoOffset -lt $headerEnd) { throw 'Invalid UnityFS metadata offset' }
        $stream.Position = $infoOffset
        $metadata = Expand-UnityBlock (Read-Bytes $reader $packedInfo) ($flags -band 0x3f) $plainInfo
        $blockInfo = [IO.BinaryReader]::new([IO.MemoryStream]::new($metadata))
        $sha = [Security.Cryptography.SHA256]::Create()
        try {
            $blockInfo.BaseStream.Position = 16
            $count = Read-BigEndian $blockInfo 4
            if ($count -lt 1 -or $count -gt 4096) { throw 'Invalid block count' }
            $stream.Position = if ($infoAtEnd) { $headerEnd } else { $infoOffset + $packedInfo }
            if ($flags -band 0x200) { $stream.Position = ($stream.Position + 15) -band (-bnot 15) }
            $dataEnd = if ($infoAtEnd) { $infoOffset } else { $stream.Length }
            for ($index = 0; $index -lt $count; $index++) {
                $plain = Read-BigEndian $blockInfo 4
                $packed = Read-BigEndian $blockInfo 4
                $mode = (Read-BigEndian $blockInfo 2) -band 0x3f
                if ($packed -gt ($dataEnd - $stream.Position)) { throw 'Truncated UnityFS block' }
                $block = Expand-UnityBlock (Read-Bytes $reader $packed) $mode $plain
                $null = $sha.TransformBlock($block, 0, $block.Length, $block, 0)
            }
            $null = $sha.TransformFinalBlock([byte[]]::new(0), 0, 0)
            return ([BitConverter]::ToString($sha.Hash)).Replace('-', '').ToLowerInvariant()
        } finally {
            $sha.Dispose()
            $blockInfo.Dispose()
        }
    } finally {
        $reader.Dispose()
    }
}

if (-not $Path -or $Remaining.Count -ne 0) {
    Write-Output 'Usage: .\show_content_fingerprint.ps1 path/to/cards/card_data'
    exit 2
}

Get-UnityFingerprint (Resolve-Path -LiteralPath $Path).ProviderPath


