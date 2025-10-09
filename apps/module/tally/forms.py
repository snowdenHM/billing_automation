from django import forms
from django.db.models import Q
from .models import TallyConfig, ParentLedger


class TallyConfigForm(forms.ModelForm):
    """
    Custom form for TallyConfig that handles organization-dependent parent ledger filtering
    """

    class Meta:
        model = TallyConfig
        fields = '__all__'
        widgets = {
            'igst_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'igst_parents'
            }),
            'cgst_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'cgst_parents'
            }),
            'sgst_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'sgst_parents'
            }),
            'vendor_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'vendor_parents'
            }),
            'chart_of_accounts_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'chart_of_accounts_parents'
            }),
            'chart_of_accounts_expense_parents': forms.SelectMultiple(attrs={
                'class': 'form-control',
                'style': 'height: 200px;',
                'data-field': 'chart_of_accounts_expense_parents'
            }),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Safely get organization from various sources
        organization = None

        # Try to get organization from existing instance (edit mode)
        if self.instance and hasattr(self.instance, 'pk') and self.instance.pk:
            try:
                if hasattr(self.instance, 'organization_id') and self.instance.organization_id:
                    organization = self.instance.organization
            except Exception:
                # Handle any RelatedObjectDoesNotExist or other exceptions
                organization = None

        # Try to get organization from initial data (pre-populated forms)
        if not organization and hasattr(self, 'initial') and 'organization' in self.initial:
            organization = self.initial['organization']

        # Try to get organization from POST data (form submissions)
        if not organization and hasattr(self, 'data') and self.data and 'organization' in self.data:
            try:
                from apps.organizations.models import Organization
                org_id = self.data['organization']
                if org_id:
                    organization = Organization.objects.get(pk=org_id)
            except (Organization.DoesNotExist, ValueError, TypeError):
                organization = None

        # Filter parent ledger querysets based on organization
        if organization:
            parent_ledger_queryset = ParentLedger.objects.filter(organization=organization).order_by('parent')
        else:
            parent_ledger_queryset = ParentLedger.objects.none()

        # Set the queryset for all parent ledger fields
        parent_fields = [
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        ]

        for field_name in parent_fields:
            if field_name in self.fields:
                self.fields[field_name].queryset = parent_ledger_queryset
                self.fields[field_name].widget.attrs.update({
                    'multiple': True,
                    'size': '10'
                })

    def clean(self):
        cleaned_data = super().clean()
        organization = cleaned_data.get('organization')

        if organization:
            # Validate that all selected parent ledgers belong to the selected organization
            parent_fields = [
                'igst_parents',
                'cgst_parents',
                'sgst_parents',
                'vendor_parents',
                'chart_of_accounts_parents',
                'chart_of_accounts_expense_parents'
            ]

            for field_name in parent_fields:
                selected_parents = cleaned_data.get(field_name)
                if selected_parents and hasattr(selected_parents, 'exclude'):
                    invalid_parents = selected_parents.exclude(organization=organization)
                    if invalid_parents.exists():
                        raise forms.ValidationError(
                            f"All {field_name.replace('_', ' ')} must belong to the selected organization."
                        )

        return cleaned_data
