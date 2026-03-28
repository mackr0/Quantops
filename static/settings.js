/**
 * QuantOpsAI Settings Page JavaScript
 * - Slider value display
 * - Test Connection AJAX
 * - Reset to defaults
 */

document.addEventListener('DOMContentLoaded', function () {

    // -----------------------------------------------------------------------
    // Slider value display: show current value next to each range input
    // -----------------------------------------------------------------------
    document.querySelectorAll('input[type="range"].slider').forEach(function (slider) {
        var displayId = slider.dataset.display;
        var format = slider.dataset.format || 'pct';
        var suffix = slider.dataset.suffix || '';

        function updateDisplay() {
            var output = document.getElementById(displayId);
            if (!output) return;

            var val = parseFloat(slider.value);
            var text;

            if (format === 'int') {
                text = Math.round(val).toString();
                if (!suffix) suffix = '%';
            } else if (format === '1f') {
                text = val.toFixed(1);
                if (!suffix) suffix = '';
            } else {
                // Default: percentage (value is 0.03 -> display as 3.0%)
                text = (val * 100).toFixed(1);
                suffix = '%';
            }

            output.textContent = text + suffix;
        }

        slider.addEventListener('input', updateDisplay);
        // Initialize display
        updateDisplay();
    });

    // -----------------------------------------------------------------------
    // Test Alpaca Connection
    // -----------------------------------------------------------------------
    var testBtn = document.getElementById('test-connection-btn');
    if (testBtn) {
        testBtn.addEventListener('click', function () {
            var resultDiv = document.getElementById('test-result');
            resultDiv.style.display = 'block';
            resultDiv.innerHTML = '<span class="muted">Testing connection...</span>';
            testBtn.disabled = true;
            testBtn.setAttribute('aria-busy', 'true');

            fetch('/settings/keys/test', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
            })
                .then(function (resp) { return resp.json(); })
                .then(function (data) {
                    if (data.success) {
                        resultDiv.innerHTML =
                            '<div class="flash flash-success">' + data.message + '</div>';
                    } else {
                        resultDiv.innerHTML =
                            '<div class="flash flash-error">' + data.message + '</div>';
                    }
                })
                .catch(function (err) {
                    resultDiv.innerHTML =
                        '<div class="flash flash-error">Request failed: ' + err.message + '</div>';
                })
                .finally(function () {
                    testBtn.disabled = false;
                    testBtn.removeAttribute('aria-busy');
                });
        });
    }

    // -----------------------------------------------------------------------
    // Reset to Defaults buttons
    // -----------------------------------------------------------------------
    document.querySelectorAll('.reset-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var segment = this.dataset.segment;
            var name = this.dataset.name;
            if (confirm('Reset ' + name + ' configuration to defaults?')) {
                // Create a form and POST to the reset endpoint
                var form = document.createElement('form');
                form.method = 'POST';
                form.action = '/settings/segment/' + segment + '/reset';
                document.body.appendChild(form);
                form.submit();
            }
        });
    });
});
