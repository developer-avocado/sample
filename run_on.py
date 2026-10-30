for /f "delims=" %i in ('az functionapp keys list --name <アプリ名> --resource-group <リソースグループ名> --query masterKey -o tsv') do set "KEY=%i"

curl -i -X POST "https://<アプリ名>.azurewebsites.net/admin/functions/<関数名>" -H "x-functions-key: %KEY%" -H "Content-Type: application/json" -d "{}"