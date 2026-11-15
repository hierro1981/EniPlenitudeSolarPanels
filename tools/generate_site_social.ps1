# Optional Windows-only utility; the website itself has no build dependencies.
Add-Type -AssemblyName System.Drawing
$bitmap = New-Object System.Drawing.Bitmap 1200, 630
$graphics = [System.Drawing.Graphics]::FromImage($bitmap)
$graphics.SmoothingMode = [System.Drawing.Drawing2D.SmoothingMode]::AntiAlias
$graphics.TextRenderingHint = [System.Drawing.Text.TextRenderingHint]::AntiAliasGridFit
$resources = [System.Collections.Generic.List[System.IDisposable]]::new()
function Brush([string]$hex) {
    $item = [System.Drawing.SolidBrush]::new([System.Drawing.ColorTranslator]::FromHtml($hex))
    $resources.Add($item)
    return $item
}
function Font([single]$size, [System.Drawing.FontStyle]$style = [System.Drawing.FontStyle]::Regular) {
    $item = [System.Drawing.Font]::new('Segoe UI', $size, $style, [System.Drawing.GraphicsUnit]::Pixel)
    $resources.Add($item)
    return $item
}
try {
    $paper = Brush '#f4f3ec'
    $blue = Brush '#083553'
    $accent = Brush '#2185bd'
    $muted = Brush '#526875'
    $sky = Brush '#b9e4fb'
    $solar = Brush '#f4ce66'
    $graphics.FillRectangle($paper, 0, 0, 1200, 630)
    $graphics.FillRectangle($blue, 800, 0, 400, 630)
    $graphics.DrawString('RAMAYA SOFTWARE / HOME ASSISTANT', (Font 19 ([System.Drawing.FontStyle]::Bold)), $blue, 56, 50)
    $graphics.DrawString("La tua energia.", (Font 70 ([System.Drawing.FontStyle]::Bold)), $blue, 50, 125)
    $graphics.DrawString(("Una casa pi" + [char]0x00F9), (Font 70 ([System.Drawing.FontStyle]::Bold)), $blue, 50, 210)
    $graphics.DrawString("connessa.", (Font 70 ([System.Drawing.FontStyle]::Bold)), $accent, 50, 295)
    $graphics.DrawString('Plenitude Solar Panels', (Font 32 ([System.Drawing.FontStyle]::Bold)), $blue, 56, 435)
    $graphics.DrawString('Integrazione indipendente, gratuita e open source.', (Font 23), $muted, 56, 485)
    $graphics.DrawString('solar.ramaya.org', (Font 22), $blue, 56, 560)
    $graphics.FillEllipse($solar, 1025, 95, 80, 80)
    $graphics.FillRectangle($accent, 850, 255, 295, 185)
    $pen = [System.Drawing.Pen]::new([System.Drawing.ColorTranslator]::FromHtml('#b9e4fb'), 2)
    $resources.Add($pen)
    $graphics.DrawRectangle($pen, 850, 255, 295, 185)
    foreach ($x in @(909, 968, 1027, 1086)) { $graphics.DrawLine($pen, $x, 255, $x, 440) }
    foreach ($y in @(301, 347, 393)) { $graphics.DrawLine($pen, 850, $y, 1145, $y) }
    $graphics.DrawString('SOLE. DATI. CASA.', (Font 20 ([System.Drawing.FontStyle]::Bold)), $sky, 850, 490)
    $output = Join-Path $PSScriptRoot '../site/assets/social.png'
    $bitmap.Save([System.IO.Path]::GetFullPath($output), [System.Drawing.Imaging.ImageFormat]::Png)
} finally {
    foreach ($resource in $resources) { $resource.Dispose() }
    $graphics.Dispose()
    $bitmap.Dispose()
}
