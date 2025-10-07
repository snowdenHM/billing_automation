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

        // Get the AJAX URL from context
        var ajaxUrl = '/admin/tally/tallyconfig/get-parent-ledgers/';

        $.ajax({
            url: ajaxUrl,
            data: {
                'org_id': organizationId
            },
            success: function(data) {
                updateFilterHorizontalOptions(data.parent_ledgers);
            },
            error: function() {
                console.error('Failed to fetch parent ledgers');
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
                // Clear current options in from box
                fromBox.empty();

                // Add new options to from box
                parentLedgers.forEach(function(ledger) {
                    var option = new Option(ledger.parent, ledger.id);
                    fromBox.append(option);
                });

                // Keep selected items in to box, but remove any that don't exist anymore
                var selectedIds = parentLedgers.map(function(ledger) { return ledger.id.toString(); });
                toBox.find('option').each(function() {
                    if (selectedIds.indexOf($(this).val()) === -1) {
                        $(this).remove();
                    }
                });
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
    }

    // Initialize when DOM is ready
    $(document).ready(function() {
        // Handle organization change
        $('#id_organization').on('change', function() {
            var organizationId = $(this).val();
            updateParentLedgerOptions(organizationId);
        });

        // Load initial data if organization is already selected
        var initialOrgId = $('#id_organization').val();
        if (initialOrgId) {
            updateParentLedgerOptions(initialOrgId);
        }
    });

})(django.jQuery);
