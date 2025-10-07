// Tally Dependent Dropdown JavaScript
// Handles dynamic updating of parent ledger options based on organization selection

(function($) {
    'use strict';

    function updateParentLedgerOptions(organizationId) {
        if (!organizationId) {
            // Clear all parent ledger options if no organization selected
            clearAllParentLedgerOptions();
            return;
        }

        // Get the AJAX URL - use relative path that works with Django admin
        var ajaxUrl = window.location.pathname.replace(/\/add\/$/, '/get-parent-ledgers/').replace(/\/\d+\/change\/$/, '/get-parent-ledgers/');

        // Fallback to absolute URL if needed
        if (!ajaxUrl.includes('/get-parent-ledgers/')) {
            ajaxUrl = '/admin/tally/tallyconfig/get-parent-ledgers/';
        }

        $.ajax({
            url: ajaxUrl,
            data: {
                'org_id': organizationId,
                'csrfmiddlewaretoken': $('[name=csrfmiddlewaretoken]').val()
            },
            method: 'GET',
            success: function(data) {
                console.log('Parent ledgers received:', data.parent_ledgers);
                updateFilterHorizontalOptions(data.parent_ledgers);
            },
            error: function(xhr, status, error) {
                console.error('Failed to fetch parent ledgers:', status, error);
                console.error('Response:', xhr.responseText);
                clearAllParentLedgerOptions();
            }
        });
    }

    function updateFilterHorizontalOptions(parentLedgers) {
        // List of filter horizontal field names
        var fieldNames = [
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        ];

        fieldNames.forEach(function(fieldName) {
            var fromBox = $('#id_' + fieldName + '_from');
            var toBox = $('#id_' + fieldName + '_to');

            if (fromBox.length && toBox.length) {
                // Store currently selected items
                var selectedItems = [];
                toBox.find('option').each(function() {
                    selectedItems.push({
                        value: $(this).val(),
                        text: $(this).text()
                    });
                });

                // Clear current options in from box
                fromBox.empty();

                // Add new options to from box
                parentLedgers.forEach(function(ledger) {
                    var option = new Option(ledger.parent, ledger.id);
                    fromBox.append(option);
                });

                // Remove selected items that no longer exist in the organization
                var availableIds = parentLedgers.map(function(ledger) { return ledger.id.toString(); });
                toBox.find('option').each(function() {
                    if (availableIds.indexOf($(this).val()) === -1) {
                        $(this).remove();
                    }
                });

                console.log('Updated filter horizontal for field:', fieldName);
            } else {
                console.warn('Filter horizontal boxes not found for field:', fieldName);
            }
        });
    }

    function clearAllParentLedgerOptions() {
        var fieldNames = [
            'igst_parents',
            'cgst_parents',
            'sgst_parents',
            'vendor_parents',
            'chart_of_accounts_parents',
            'chart_of_accounts_expense_parents'
        ];

        fieldNames.forEach(function(fieldName) {
            var fromBox = $('#id_' + fieldName + '_from');
            var toBox = $('#id_' + fieldName + '_to');

            if (fromBox.length && toBox.length) {
                fromBox.empty();
                toBox.empty();
            }
        });
        console.log('Cleared all parent ledger options');
    }

    // Initialize when DOM is ready
    $(document).ready(function() {
        console.log('Tally dependent dropdown initialized');

        // Handle organization change
        $('#id_organization').on('change', function() {
            var organizationId = $(this).val();
            console.log('Organization changed to:', organizationId);
            updateParentLedgerOptions(organizationId);
        });

        // Load initial data if organization is already selected
        var initialOrgId = $('#id_organization').val();
        if (initialOrgId) {
            console.log('Loading initial data for organization:', initialOrgId);
            updateParentLedgerOptions(initialOrgId);
        }
    });

})(django.jQuery || jQuery);
