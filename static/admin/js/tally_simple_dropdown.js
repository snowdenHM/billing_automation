/**
 * Tally Simple Dropdown JavaScript
 * Handles dynamic updating of parent ledger options for simple select multiple widgets
 */

(function() {
    'use strict';

    // Function to initialize the script once jQuery is available
    function initializeTallySimpleDropdown() {
        // Use django.jQuery if available, otherwise fall back to window.jQuery or $
        var $ = window.django && window.django.jQuery || window.jQuery || window.$;

        if (!$) {
            console.error('jQuery not found. Cannot initialize tally simple dropdown.');
            return;
        }

        function updateParentLedgerOptions(organizationId) {
            if (!organizationId) {
                clearAllParentLedgerOptions();
                return;
            }

            // Get the AJAX URL for fetching parent ledgers
            var ajaxUrl = '/admin/tally/tallyconfig/get-parent-ledgers/';

            $.ajax({
                url: ajaxUrl,
                data: {
                    'org_id': organizationId,
                    'csrfmiddlewaretoken': $('[name=csrfmiddlewaretoken]').val()
                },
                method: 'GET',
                success: function(data) {
                    console.log('Parent ledgers received:', data.parent_ledgers);
                    updateSelectMultipleOptions(data.parent_ledgers);
                },
                error: function(xhr, status, error) {
                    console.error('Failed to fetch parent ledgers:', status, error);
                    clearAllParentLedgerOptions();
                }
            });
        }

        function updateSelectMultipleOptions(parentLedgers) {
            // List of select multiple field names
            var fieldNames = [
                'igst_parents',
                'cgst_parents',
                'sgst_parents',
                'vendor_parents',
                'chart_of_accounts_parents',
                'chart_of_accounts_expense_parents'
            ];

            fieldNames.forEach(function(fieldName) {
                var selectField = $('#id_' + fieldName);

                if (selectField.length) {
                    // Store currently selected values
                    var selectedValues = selectField.val() || [];
                    
                    // Re-enable the field and clear existing options
                    selectField.prop('disabled', false);
                    selectField.empty();

                    // Add new options
                    parentLedgers.forEach(function(ledger) {
                        var option = $('<option></option>')
                            .attr('value', ledger.id)
                            .text(ledger.parent);
                        
                        // Re-select if it was previously selected
                        if (selectedValues.includes(ledger.id.toString())) {
                            option.attr('selected', 'selected');
                        }
                        
                        selectField.append(option);
                    });

                    // Trigger change event to update any dependent widgets
                    selectField.trigger('change');
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
                var selectField = $('#id_' + fieldName);

                if (selectField.length) {
                    selectField.prop('disabled', false);
                    selectField.empty();
                    selectField.trigger('change');
                }
            });
        }

        // Add visual feedback for organization changes
        function showLoadingState(show) {
            var fieldNames = [
                'igst_parents',
                'cgst_parents',
                'sgst_parents',
                'vendor_parents',
                'chart_of_accounts_parents',
                'chart_of_accounts_expense_parents'
            ];

            fieldNames.forEach(function(fieldName) {
                var selectField = $('#id_' + fieldName);
                if (selectField.length) {
                    if (show) {
                        selectField.prop('disabled', true);
                        selectField.html('<option>Loading parent ledgers...</option>');
                    } else {
                        selectField.prop('disabled', false);
                        // Don't clear options here - let updateSelectMultipleOptions handle it
                    }
                }
            });
        }

        // Initialize when document is ready
        $(document).ready(function() {
            var organizationField = $('#id_organization');

            if (organizationField.length) {
                // Handle organization field changes
                organizationField.on('change', function() {
                    var selectedOrg = $(this).val();

                    if (selectedOrg) {
                        showLoadingState(true);
                        updateParentLedgerOptions(selectedOrg);
                    } else {
                        clearAllParentLedgerOptions();
                    }
                });

                // If organization is already selected (edit mode), update options
                var currentOrg = organizationField.val();
                if (currentOrg) {
                    updateParentLedgerOptions(currentOrg);
                }
            }
        });
    }

    // Try to initialize immediately if jQuery is available
    if (window.django && window.django.jQuery) {
        initializeTallySimpleDropdown();
    } else {
        // Wait for django admin to load
        document.addEventListener('DOMContentLoaded', function() {
            // Try multiple times with delays to ensure django.jQuery is loaded
            var attempts = 0;
            var maxAttempts = 10;

            function tryInitialize() {
                attempts++;
                if (window.django && window.django.jQuery) {
                    initializeTallySimpleDropdown();
                } else if (attempts < maxAttempts) {
                    setTimeout(tryInitialize, 100);
                } else {
                    console.error('Could not find django.jQuery after multiple attempts');
                }
            }

            tryInitialize();
        });
    }

})();
