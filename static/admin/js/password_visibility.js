/*
 * Admin password forms: reveal what is being typed and confirm the two
 * entries match before the form is submitted.
 *
 * Used by admin/auth/user/change_password.html and admin/auth/user/add_form.html.
 */
'use strict';
{
    const messages = {
        show: 'Show',
        hide: 'Hide',
        showLabel: 'Show password',
        hideLabel: 'Hide password',
        retype: 'Re-type the password above to confirm it.',
        match: '✓ Passwords match',
        mismatch: '✗ Passwords do not match'
    };

    function addToggle(input) {
        if (input.dataset.pwToggle === 'on') {
            return;
        }
        input.dataset.pwToggle = 'on';

        const button = document.createElement('button');
        button.type = 'button';
        button.className = 'pw-toggle';
        if (input.id) {
            button.setAttribute('aria-controls', input.id);
        }

        const render = function(visible) {
            input.type = visible ? 'text' : 'password';
            button.textContent = visible ? messages.hide : messages.show;
            button.setAttribute('aria-pressed', visible ? 'true' : 'false');
            button.setAttribute('aria-label', visible ? messages.hideLabel : messages.showLabel);
        };

        button.addEventListener('click', function() {
            render(input.type === 'password');
        });
        render(false);

        input.insertAdjacentElement('afterend', button);
    }

    function addMatchIndicator(first, second) {
        const row = second.closest('.form-row') || second.parentElement;
        const note = document.createElement('div');
        note.className = 'pw-match';
        note.setAttribute('role', 'status');
        note.setAttribute('aria-live', 'polite');
        row.appendChild(note);

        const update = function() {
            if (!first.value && !second.value) {
                note.className = 'pw-match';
                note.textContent = '';
            } else if (!second.value) {
                note.className = 'pw-match pw-match--idle';
                note.textContent = messages.retype;
            } else if (first.value === second.value) {
                note.className = 'pw-match pw-match--ok';
                note.textContent = messages.match;
            } else {
                note.className = 'pw-match pw-match--bad';
                note.textContent = messages.mismatch;
            }
        };

        [first, second].forEach(function(input) {
            input.addEventListener('input', update);
            input.addEventListener('change', update);
        });
        update();
    }

    document.addEventListener('DOMContentLoaded', function() {
        const scope = document.querySelector('#content-main') || document;
        scope.querySelectorAll('input[type="password"]').forEach(addToggle);

        const first = scope.querySelector('#id_password1');
        const second = scope.querySelector('#id_password2');
        if (first && second) {
            addMatchIndicator(first, second);
        }
    });
}
