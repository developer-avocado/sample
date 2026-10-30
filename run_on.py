KEY=$(az functionapp keys list --name <アプリ名> --resource-group <RG名> --query "masterKey" -o tsv)

# 管理者APIをPOSTで叩き、タイマートリガーを即座に1回キック
curl -X POST "https://<アプリ名>.azurewebsites.net/admin/functions/<関数名>" \
     -H "x-functions-key: ${KEY}" \
     -H "Content-Type: application/json" \
     -d "{}"