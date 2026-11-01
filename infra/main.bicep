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

@description('Globally unique name of the Web App managed by this deployment.')
param webAppName string

@description('Public GHCR container image including an immutable tag.')
param containerImage string

@description('Client ID of the Microsoft app registration shared with production Easy Auth.')
param microsoftAuthClientId string

@secure()
@description('Client secret of the Microsoft app registration shared with production Easy Auth.')
param microsoftProviderAuthenticationSecret string

@secure()
@description('Key used to sign application cookies.')
param secretKey string

@secure()
@description('Microsoft account preapproved as this deployment\'s application administrator.')
param adminEmail string

@description('Whether App Service client-affinity cookies are enabled.')
param clientAffinityEnabled bool = false

var isFreeTier = appServicePlanSku == 'F1'
var appServicePlanTier = isFreeTier ? 'Free' : 'Basic'
var appServicePlanFamily = isFreeTier ? 'F' : 'B'
var webAppUrl = 'https://${webAppName}.azurewebsites.net'

// The same desired plan definition is used by the test and production parameter
// files. Each incremental deployment then manages its selected Web App normally.
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

resource webApp 'Microsoft.Web/sites@2024-11-01' = {
  name: webAppName
  location: location
  kind: 'app,linux,container'
  properties: {
    serverFarmId: appServicePlan.id
    httpsOnly: true
    clientAffinityEnabled: clientAffinityEnabled
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: 'DOCKER|${containerImage}'
      alwaysOn: !isFreeTier
      ftpsState: 'Disabled'
      http20Enabled: true
      minTlsVersion: '1.2'
    }
  }
}

// Declaring the complete map as a child resource makes Bicep authoritative for
// settings and removes obsolete password and private-registry credentials.
resource appSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: webApp
  name: 'appsettings'
  properties: {
    ADMIN_COOKIE_SECURE: '1'
    ADMIN_EMAIL: adminEmail
    AUTH_MODE: 'easy_auth'
    MICROSOFT_AUTH_CLIENT_ID: microsoftAuthClientId
    MICROSOFT_PROVIDER_AUTHENTICATION_SECRET: microsoftProviderAuthenticationSecret
    PUBLIC_BASE_URL: webAppUrl
    PUBLIC_GOODS_DB_PATH: '/home/public_goods.db'
    SECRET_KEY: secretKey
    WEBSITES_ENABLE_APP_SERVICE_STORAGE: 'true'
    WEBSITES_PORT: '8000'
  }
}

resource easyAuth 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: webApp
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

resource scmPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: webApp
  name: 'scm'
  properties: {
    allow: false
  }
}

resource ftpPublishingPolicy 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: webApp
  name: 'ftp'
  properties: {
    allow: false
  }
}

output webAppName string = webApp.name
output webAppUrl string = webAppUrl
output appServicePlanName string = appServicePlan.name
output containerImage string = containerImage
