using './main.bicep'

var config = loadJsonContent('deployment.config.json')

param location = config.location
param appServicePlanName = config.appServicePlanName
param appServicePlanSku = config.appServicePlanSku
param webAppName = config.testWebAppName
param containerImage = readEnvironmentVariable('CONTAINER_IMAGE')
param microsoftAuthClientId = readEnvironmentVariable('MICROSOFT_AUTH_CLIENT_ID')
param microsoftProviderAuthenticationSecret = readEnvironmentVariable('TEST_MICROSOFT_PROVIDER_AUTHENTICATION_SECRET')
param secretKey = readEnvironmentVariable('TEST_SECRET_KEY')
param adminEmail = readEnvironmentVariable('TEST_ADMIN_EMAIL')
