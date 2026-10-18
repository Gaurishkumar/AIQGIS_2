# Sets up the local AI/GIS worker on an operator PC (Windows, PowerShell).
#
#   powershell -ExecutionPolicy Bypass -File tools\setup_worker.ps1 [-SkipModels] [-Cuda]
#
# 1. Creates worker\.venv with Python 3.12 (via uv), separate from QGIS's Python.
# 2. Installs the pinned requirements (CPU PyTorch by default).
# 3. Downloads the SAM 2.1 checkpoints and records their SHA-256 in data\models\manifest.json.
# The worker then runs offline; only reference-data preparation needs internet (once per area).

param([switch]$SkipModels, [switch]$Cuda)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

python -m pip install --quiet --upgrade uv
python -m uv venv worker\.venv --python 3.12
$py = Join-Path $root "worker\.venv\Scripts\python.exe"

$torchIndex = if ($Cuda) { "https://download.pytorch.org/whl/cu124" } else { "https://download.pytorch.org/whl/cpu" }
python -m uv pip install --python $py torch torchvision --index-url $torchIndex
$env:SAM2_BUILD_CUDA = "0"
python -m uv pip install --python $py -r worker\requirements.txt

if (-not $SkipModels) {
    $models = Join-Path $root "data\models"
    New-Item -ItemType Directory -Force $models | Out-Null
    $base = "https://dl.fbaipublicfiles.com/segment_anything_2/092824"
    $manifest = @{ files = @{} }
    foreach ($m in @("sam2.1_hiera_small.pt", "sam2.1_hiera_base_plus.pt")) {
        $dst = Join-Path $models $m
        if (-not (Test-Path $dst)) { Invoke-WebRequest -Uri "$base/$m" -OutFile $dst }
        $hash = (Get-FileHash -Algorithm SHA256 $dst).Hash.ToLower()
        $manifest.files[$m] = @{ sha256 = $hash; source = "$base/$m"; licence = "Apache-2.0" }
    }
    $manifest | ConvertTo-Json -Depth 5 | Set-Content -Encoding utf8 (Join-Path $models "manifest.json")
}

& $py -c "import bpa_worker, torch, sam2, rasterio, geopandas, fastapi; print('worker environment OK, torch', torch.__version__)"
Write-Host "Done. Next: python -m bpa_worker prepare-reference --config config\config.yaml --excel <workbook.xlsx>  (from the worker folder)"
