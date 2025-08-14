from django.test import TestCase
from rest_framework.test import APIClient
from .models import CustomUser

class UserTests(TestCase):
    def setUp(self):
        self.client = APIClient()
        self.user_data = {
            'email': 'test@example.com',
            'first_name': 'Test',
            'last_name': 'User',
            'password': 'testpass123',
            'password_confirm': 'testpass123'
        }

    def test_register_user(self):
        response = self.client.post('/api/user/register/', self.user_data)
        self.assertEqual(response.status_code, 201)
        self.assertEqual(CustomUser.objects.count(), 1)
        self.assertEqual(CustomUser.objects.get().email, 'test@example.com')

    def test_login_user(self):
        CustomUser.objects.create_user(email='test@example.com', password='testpass123')
        response = self.client.post('/api/user/login/', {
            'email': 'test@example.com',
            'password': 'testpass123'
        })
        self.assertEqual(response.status_code, 200)
        self.assertIn('message', response.data)
        self.assertEqual(response.data['message'], 'Login successful')