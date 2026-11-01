using './main.bicep'

var config = loadJsonContent('deployment.config.json')

param location = config.location
param appServicePlanName = config.appServicePlanName
param appServicePlanSku = config.appServicePlanSku
param webAppName = config.productionWebAppName
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param microsoftAuthClientId = readEnvironmentVariable('MICROSOFT_AUTH_CLIENT_ID')
param microsoftProviderAuthenticationSecret = readEnvironmentVariable('PRODUCTION_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET')
param secretKey = readEnvironmentVariable('PRODUCTION_SECRET_KEY')
param adminEmail = readEnvironmentVariable('PRODUCTION_ADMIN_EMAIL')
param clientAffinityEnabled = true
