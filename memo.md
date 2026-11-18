CREATE USER [<Functionsのアプリ名>] FROM EXTERNAL PROVIDER;
ALTER ROLE db_datareader ADD MEMBER [<Functionsのアプリ名>];
ALTER ROLE db_datawriter ADD MEMBER [<Functionsのアプリ名>];