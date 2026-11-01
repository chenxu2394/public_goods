# Authentication and identity

## Authentication model

- Admins and teachers sign in with preapproved personal Microsoft accounts
  through Azure App Service Easy Auth.
- Students do not create accounts. They open a session join URL and enter a
  student number and name that exactly match the uploaded roster.
- The application stores roles and session ownership locally; Microsoft handles
  instructor authentication.
- Azure leaves public student and display routes anonymous, while the
  application protects administrative routes.

Easy Auth injects the authenticated principal into
`X-MS-CLIENT-PRINCIPAL`. The application trusts that header only when deployed
behind Azure App Service Easy Auth.

## Azure ownership

The authentication system crosses two management APIs:

| Configuration | Owner |
| --- | --- |
| App Service `authsettingsV2` | `infra/main.bicep` |
| Authentication-related App Settings | `infra/main.bicep` |
| Microsoft Entra application registration | `scripts/configure_azure_easy_auth.sh` |
| Entra callback URLs and client-secret creation | `scripts/configure_azure_easy_auth.sh` |
| Secure values consumed by Bicep | Encrypted GitHub Actions secrets |

The Graph maintenance script deliberately does not update App Service settings
or Easy Auth directly. After it changes a client ID, administrator identity, or
provider secret, the appropriate GitHub Actions deployment applies those values
through Bicep.

## Required App Settings

Bicep maintains the complete App Settings map, including:

- `AUTH_MODE=easy_auth`
- `ADMIN_EMAIL`, supplied through an encrypted environment-specific GitHub secret
- `MICROSOFT_AUTH_CLIENT_ID`, supplied through a GitHub variable
- `MICROSOFT_PROVIDER_AUTHENTICATION_SECRET`, supplied through an encrypted
  environment-specific GitHub secret
- `PUBLIC_BASE_URL`, derived from the Web App name
- `PUBLIC_GOODS_DB_PATH=/home/public_goods.db`

`ADMIN_EMAIL` preapproves the initial administrator. After the first successful
binding, the immutable Microsoft subject ID is the identity source of truth;
matching the email text alone is insufficient.

## Microsoft Entra application registration

The application registration is a Microsoft Graph object rather than an Azure
Resource Manager object. Maintain it with:

```bash
./scripts/configure_azure_easy_auth.sh
```

The script:

- creates or reuses the dedicated personal-Microsoft-account app registration
- preserves existing redirect URIs and ensures both production and test callback
  URLs are present
- enables ID-token issuance
- writes the client ID and administrator identity to GitHub Actions
- creates a client secret only for a new registration, an absent GitHub secret,
  or an explicitly requested rotation
- never writes App Service settings or `authsettingsV2` directly

It loads `ADMIN_EMAIL` from the ignored local `.env` file unless the value is
already present in the process environment. No personal email or secret is
stored in the repository.

To append and activate a new two-year client secret:

```bash
ROTATE_MICROSOFT_CLIENT_SECRET=1 \
./scripts/configure_azure_easy_auth.sh
```

After a registration or secret change, apply the values to both Web Apps:

```bash
gh workflow run deploy-azure-test.yml --ref main -f operation=create
gh workflow run deploy-azure-production.yml --ref main -f operation=create
```

The test app can be stopped again after verification.

## Existing password-admin migration

Switching an existing database from `AUTH_MODE=password` to
`AUTH_MODE=easy_auth` does not create a second administrator:

1. The existing local user named `admin` keeps its database user ID and session
   ownership.
2. On startup, the application adds `ADMIN_EMAIL` to that user if no Microsoft
   identity is linked yet.
3. On the first successful Microsoft sign-in, the immutable Microsoft subject
   ID is linked to the same user.
4. Later sign-ins must match the linked Microsoft identity.

The old password hash can remain in the row for migration safety, but password
login is ignored while `AUTH_MODE=easy_auth`.
