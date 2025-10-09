/**
 * Tally Dependent Dropdown JavaScript
 * Handles dynamic updating of parent ledger options based on organization selection
 */

(function() {
    'use strict';

    // Function to initialize the script once jQuery is available
    function initializeTallyDropdown() {
        // Use django.jQuery if available, otherwise fall back to window.jQuery or $
        var $ = window.django && window.django.jQuery || window.jQuery || window.$;

        if (!$) {
            console.error('jQuery not found. Cannot initialize tally dependent dropdown.');
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
                    updateFilterHorizontalOptions(data.parent_ledgers);
                },
                error: function(xhr, status, error) {
                    console.error('Failed to fetch parent ledgers:', status, error);
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
                var selectFrom = $('#id_' + fieldName + '_from');
                var selectTo = $('#id_' + fieldName + '_to');

                if (selectFrom.length && selectTo.length) {
                    // Store currently selected items in the "to" list
                    var selectedItems = [];
                    selectTo.find('option').each(function() {
                        selectedItems.push({
                            id: $(this).val(),
                            text: $(this).text()
                        });
                    });

                    // Clear existing options in "from" list only
                    selectFrom.empty();

                    // Add new options to "from" list
                    parentLedgers.forEach(function(ledger) {
                        // Don't add if already selected in "to" list
                        var alreadySelected = selectedItems.some(function(item) {
                            return item.id === ledger.id.toString();
                        });

                        if (!alreadySelected) {
                            var option = $('<option></option>')
                                .attr('value', ledger.id)
                                .text(ledger.parent);
                            selectFrom.append(option);
                        }
                    });

                    // Only use SelectBox if it exists and the elements are properly initialized
                    if (window.SelectBox && selectFrom[0] && selectTo[0]) {
                        try {
                            // Check if SelectBox cache exists for these elements
                            var fromId = fieldName + '_from';
                            var toId = fieldName + '_to';

                            // Only call redisplay if the SelectBox is already initialized
                            if (SelectBox.cache[fromId]) {
                                SelectBox.redisplay(fromId);
                            }
                            if (SelectBox.cache[toId]) {
                                SelectBox.redisplay(toId);
                            }
                        } catch (e) {
                            console.warn('SelectBox operation failed for ' + fieldName + ':', e);
                        }
                    }

                    // Trigger change event to update the widget
                    selectFrom.trigger('change');
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
                var selectFrom = $('#id_' + fieldName + '_from');

                if (selectFrom.length) {
                    selectFrom.empty();

                    // Only refresh SelectBox if it exists and is properly initialized
                    if (window.SelectBox && selectFrom[0]) {
                        try {
                            var fromId = fieldName + '_from';
                            if (SelectBox.cache[fromId]) {
                                SelectBox.redisplay(fromId);
                            }
                        } catch (e) {
                            console.warn('SelectBox redisplay failed for ' + fieldName + ':', e);
                        }
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
                    updateParentLedgerOptions(selectedOrg);
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
        initializeTallyDropdown();
    } else {
        // Wait for django admin to load
        document.addEventListener('DOMContentLoaded', function() {
            // Try multiple times with delays to ensure django.jQuery is loaded
            var attempts = 0;
            var maxAttempts = 10;

            function tryInitialize() {
                attempts++;
                if (window.django && window.django.jQuery) {
                    initializeTallyDropdown();
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
