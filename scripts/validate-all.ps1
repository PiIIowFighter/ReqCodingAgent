$ErrorActionPreference = 'Stop'
& py -3.11 -m evalsys.cli validate-all @Args
exit $LASTEXITCODE
