# start-local.ps1 — PowerShell equivalent of: bash build.sh local
$ROOT = $PSScriptRoot
$SERVICES_DIR = "$ROOT\services"

# ---- 1) Generate proto stubs if missing ----
if (-not (Test-Path "$ROOT\proto_gen\pipeline_pb2.py")) {
    Write-Host ">>> proto stubs not found, generating..."

    $OUT_DIR = "$ROOT\generated"
    if (Test-Path $OUT_DIR) { Remove-Item $OUT_DIR -Recurse -Force }
    New-Item -ItemType Directory -Path $OUT_DIR | Out-Null

    python -m grpc_tools.protoc `
        -I "$ROOT" `
        --python_out="$OUT_DIR" `
        --pyi_out="$OUT_DIR" `
        --grpc_python_out="$OUT_DIR" `
        "$ROOT\pipeline.proto"

    # Fix import path in generated grpc file
    $grpcFile = "$OUT_DIR\pipeline_pb2_grpc.py"
    (Get-Content $grpcFile) -replace '^import pipeline_pb2', 'from . import pipeline_pb2' |
        Set-Content $grpcFile -Encoding utf8

    "" | Out-File "$OUT_DIR\__init__.py" -Encoding utf8

    # Distribute to all proto_gen targets
    $targets = @(
        "$ROOT\proto_gen",
        "$SERVICES_DIR\gateway\proto_gen",
        "$SERVICES_DIR\pedestrian-service\proto_gen",
        "$SERVICES_DIR\vehicle-service\proto_gen"
    )
    foreach ($t in $targets) {
        if (Test-Path $t) { Remove-Item $t -Recurse -Force }
        Copy-Item $OUT_DIR $t -Recurse
        Write-Host "  copied to $($t.Replace($ROOT, ''))"
    }
    Write-Host ">>> proto stubs generated."
}

# ---- 2) Start all services ----
$env:PYTHONPATH = $ROOT

Write-Host "Starting pedestrian-service on :50052..."
$ped = Start-Process python -ArgumentList "server.py" `
    -WorkingDirectory "$SERVICES_DIR\pedestrian-service" `
    -PassThru -NoNewWindow

Write-Host "Starting vehicle-service on :50053..."
$veh = Start-Process python -ArgumentList "server.py" `
    -WorkingDirectory "$SERVICES_DIR\vehicle-service" `
    -PassThru -NoNewWindow

Start-Sleep 3

Write-Host "Starting router-service on :50051..."
$rtr = Start-Process python -ArgumentList "server.py" `
    -WorkingDirectory "$SERVICES_DIR\router-service" `
    -PassThru -NoNewWindow

Start-Sleep 3

Write-Host "Starting gateway on :5000..."
$gw = Start-Process python -ArgumentList "server.py" `
    -WorkingDirectory "$SERVICES_DIR\gateway" `
    -PassThru -NoNewWindow

Write-Host ""
Write-Host "All services running:"
Write-Host "  Gateway:    http://localhost:5000   (PID: $($gw.Id))"
Write-Host "  Router:     :50051                 (PID: $($rtr.Id))"
Write-Host "  Pedestrian: :50052                 (PID: $($ped.Id))"
Write-Host "  Vehicle:    :50053                 (PID: $($veh.Id))"
Write-Host ""
Write-Host "Press Ctrl+C to stop all"

try {
    # Keep script alive; Ctrl+C triggers finally
    while ($true) { Start-Sleep 5 }
} finally {
    Write-Host "Stopping all services..."
    foreach ($p in @($ped, $veh, $rtr, $gw)) {
        if (-not $p.HasExited) { $p.Kill() }
    }
}
