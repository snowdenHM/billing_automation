# API Key Authentication Guide for Bill Munshi

## 1. Update your DRF settings

Edit your `config/settings/base.py` file to include the API key authentication class:

```python
REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework_api_key.authentication.APIKeyAuthentication",  # Add this line
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    # ... other settings ...
}
```

## 2. Testing your API key authentication

After making this change, try your API request again:

```python
import requests

url = "http://localhost:8000/api/v1/tally/org/3d3d7307-b6d8-46a8-92c7-ad13167027f3/configs/"

payload = {}
headers = {
  'Authorization': 'Api-Key a882NzON.CeayqMzopCjSfBYTSntjpbPq6rIMhscC'
}

response = requests.request("GET", url, headers=headers, data=payload)

print(response.text)
```

## Important Note on API Key Format

Make sure your API key follows the correct format:
- The header should be `Authorization: Api-Key YOUR_API_KEY`
- Note the space between "Api-Key" and your key
- No extra spaces or characters

## Troubleshooting

If you're still having issues:

1. Check if the API key exists in your database:
   ```
   python manage.py shell
   from apps.organizations.models import OrganizationAPIKey
   OrganizationAPIKey.objects.filter(api_key="a882NzON.CeayqMzopCjSfBYTSntjpbPq6rIMhscC").exists()
   ```

2. Verify the organization ID is correct:
   ```
   from apps.organizations.models import Organization
   Organization.objects.filter(id="3d3d7307-b6d8-46a8-92c7-ad13167027f3").exists()
   ```

3. Check if the API key has permissions to access the organization:
   ```
   api_key = OrganizationAPIKey.objects.get(api_key="a882NzON.CeayqMzopCjSfBYTSntjpbPq6rIMhscC")
   org_id = "3d3d7307-b6d8-46a8-92c7-ad13167027f3"
   api_key.organization.id == org_id
   ```
