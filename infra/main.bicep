targetScope = 'resourceGroup'

@description('Azure region used by the existing Public Goods resources.')
param location string = resourceGroup().location

@description('Name of the App Service plan shared by the production and test apps.')
param appServicePlanName string

@description('App Service plan SKU. The existing plan currently uses F1.')
@allowed([
  'F1'
  'B1'
])
param appServicePlanSku string = 'F1'

@description('Globally unique name of the test Web App.')
param testWebAppName string

@description('Public GHCR container image including an immutable tag.')
param containerImage string

@description('Client ID of the Microsoft app registration shared with production Easy Auth.')
param microsoftAuthClientId string

@secure()
@description('Client secret of the Microsoft app registration shared with production Easy Auth.')
param microsoftProviderAuthenticationSecret string

@secure()
@description('Test-only key used to sign application cookies.')
param secretKey string

@secure()
@description('Microsoft account preapproved as the application administrator.')
param adminEmail string

var isFreeTier = appServicePlanSku == 'F1'
var appServicePlanTier = isFreeTier ? 'Free' : 'Basic'
var appServicePlanFamily = isFreeTier ? 'F' : 'B'
var testWebAppUrl = 'https://${testWebAppName}.azurewebsites.net'

// This root template is the single owner of the shared plan. Later production
// adoption should be added here rather than declaring the plan in another root.
resource appServicePlan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: appServicePlanName
  location: location
  kind: 'linux'
  sku: {
    name: appServicePlanSku
    tier: appServicePlanTier
    size: appServicePlanSku
    family: appServicePlanFamily
    capacity: 1
  }
  properties: {
    reserved: true
    perSiteScaling: false
    maximumElasticWorkerCount: 1
    zoneRedundant: false
    elasticScaleEnabled: false
  }
}

resource testWebApp 'Microsoft.Web/sites@2024-11-01' = {
  name: testWebAppName
  location: location
  kind: 'app,linux,container'
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    clientAffinityEnabled: false
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: 'DOCKER|${containerImage}'
      alwaysOn: !isFreeTier
      ftpsState: 'Disabled'
      http20Enabled: true
      minTlsVersion: '1.2'
      appSettings: [
        {
          name: 'ADMIN_COOKIE_SECURE'
          value: '1'
        }
        {
          name: 'ADMIN_EMAIL'
          value: adminEmail
        }
        {
          name: 'AUTH_MODE'
          value: 'easy_auth'
        }
        {
          name: 'MICROSOFT_AUTH_CLIENT_ID'
          value: microsoftAuthClientId
        }
        {
          name: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          value: microsoftProviderAuthenticationSecret
        }
        {
          name: 'PUBLIC_BASE_URL'
          value: testWebAppUrl
        }
        {
          name: 'PUBLIC_GOODS_DB_PATH'
          value: '/home/public_goods.db'
        }
        {
          name: 'SECRET_KEY'
          value: secretKey
        }
        {
          name: 'WEBSITES_ENABLE_APP_SERVICE_STORAGE'
          value: 'true'
        }
        {
          name: 'WEBSITES_PORT'
          value: '8000'
        }
      ]
    }
  }
}

resource testEasyAuth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: testWebApp
  name: 'authsettingsV2'
  properties: {
    platform: {
      enabled: true
      runtimeVersion: '~1'
    }
    globalValidation: {
      requireAuthentication: false
      unauthenticatedClientAction: 'AllowAnonymous'
    }
    identityProviders: {
      azureActiveDirectory: {
        enabled: true
        registration: {
          clientId: microsoftAuthClientId
          clientSecretSettingName: 'MICROSOFT_PROVIDER_AUTHENTICATION_SECRET'
          openIdIssuer: '${environment().authentication.loginEndpoint}9188040d-6c67-4c5b-b112-36a304b66dad/v2.0'
        }
      }
    }
    login: {
      tokenStore: {
        enabled: true
        tokenRefreshExtensionHours: 72
      }
    }
    httpSettings: {
      requireHttps: true
      routes: {
        apiPrefix: '/.auth'
      }
      forwardProxy: {
        convention: 'NoProxy'
      }
    }
  }
}

resource testScmPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: testWebApp
  name: 'scm'
  properties: {
    allow: false
  }
}

resource testFtpPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: testWebApp
  name: 'ftp'
  properties: {
    allow: false
  }
}

output testWebAppName string = testWebApp.name
output testWebAppUrl string = testWebAppUrl
output appServicePlanName string = appServicePlan.name
output containerImage string = containerImage
