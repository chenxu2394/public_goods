# Authentication and identity

Run the commands in this guide from the repository root.

## Authentication model

- Admins and teachers sign in with preapproved personal Microsoft accounts through Azure App Service Easy Auth.
- Students do not create accounts. They scan the session QR code or open its join URL, then enter a student number and name that exactly match the session whitelist.
- The application stores admin/teacher roles and session ownership locally; Microsoft handles authentication.
- The initial admin email is read from the ignored local `.env` file and uploaded to Azure App Settings by the setup scripts.

### Existing password admin migration

Switching an existing production database from `AUTH_MODE=password` to `AUTH_MODE=easy_auth` does not create a second admin:

1. The existing local user named `admin` keeps the same database user ID and all existing session ownership.
2. On startup, the app adds the account configured as `ADMIN_EMAIL` to that user if no Microsoft identity is linked yet.
3. On the first successful Microsoft sign-in, the immutable Microsoft subject ID is linked to that same local user.
4. Later sign-ins must match that linked Microsoft identity. Matching the email text alone is no longer sufficient.

The old password hash can remain in the row for migration safety, but password login is ignored while `AUTH_MODE=easy_auth`.

## Azure App Settings (required)

The setup scripts apply these settings through Azure CLI:

- `AUTH_MODE=easy_auth`
- `ADMIN_EMAIL`: loaded from the local `.env` file by the setup scripts
- `PUBLIC_BASE_URL`: public HTTPS origin for the app
- `PUBLIC_GOODS_DB_PATH=/home/public_goods.db`

`ADMIN_EMAIL` preapproves and binds the initial admin on first Microsoft sign-in. After that first binding, the immutable Microsoft identity is the source of truth rather than the email address.

## Configure Azure Easy Auth with Azure CLI

Azure configuration is intentionally CLI-first. The checked-in authentication template is `.azure/easy-auth.json.template`; no Azure Portal steps are required.

For a Web App that already exists:

```bash
./scripts/configure_azure_easy_auth.sh
```

Before running it, copy `.env.example` to the ignored `.env` file and set `ADMIN_EMAIL` to the initial admin's personal Microsoft account. The script queries the active Azure CLI subscription at run time. The baked-in resource defaults are resource group `PublicGoods` and Web App `public-goods`. With no arguments, review the displayed target and press Enter to continue. Positional arguments remain available for another deployment.

The script:

- creates or reuses a dedicated app registration with `PersonalMicrosoftAccount` as its audience
- configures `https://<webapp-name>.azurewebsites.net/.auth/login/aad/callback`
- creates the client secret only when needed and stores it as a slot-sticky App Service setting
- uploads `AUTH_MODE` and `ADMIN_EMAIL` from local configuration to Azure App Settings
- applies and verifies App Service Authentication V2 through `az rest`
- allows anonymous requests at the Azure edge so student/display routes remain public; the app protects `/admin/*`

The script is safe to rerun. To intentionally append and activate a new two-year client secret:

```bash
ROTATE_MICROSOFT_CLIENT_SECRET=1 \
./scripts/configure_azure_easy_auth.sh
```

Easy Auth injects the authenticated principal into `X-MS-CLIENT-PRINCIPAL`. Only trust this mode when the application is reachable through Azure App Service Easy Auth.
