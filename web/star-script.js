/**
 * Twinkling Star Script - Non-blocking async loader
 * Creates a canvas-based twinkling star effect
 */
(function() {
    'use strict';

    // Store star instances for cleanup if needed
    window.TwinklingStars = window.TwinklingStars || [];

    /**
     * Create a twinkling star canvas and inject into target element
     * @param {string|HTMLElement} target - CSS selector or DOM element
     * @param {Object} options - Configuration options
     */
    function createTwinklingStar(target, options) {
        const defaults = {
            size: 48,        // Canvas size in pixels
            speed: 1         // Animation speed multiplier
        };
        const config = Object.assign({}, defaults, options || {});

        // Get target element
        const container = typeof target === 'string' ? document.querySelector(target) : target;
        if (!container) {
            console.warn('TwinklingStar: Target not found:', target);
            return null;
        }

        // Create canvas
        const canvas = document.createElement('canvas');
        canvas.className = 'twinkling-star-canvas';
        canvas.width = config.size;
        canvas.height = config.size;
        canvas.style.display = 'inline-block';
        canvas.style.verticalAlign = 'middle';

        // Style for smooth sizing
        canvas.style.width = 'auto';
        canvas.style.height = 'auto';
        canvas.style.maxWidth = config.size + 'px';
        canvas.style.maxHeight = config.size + 'px';

        // Hide container's original content (like ✨ emoji)
        const originalContent = container.innerHTML;
        container.innerHTML = '';
        container.style.display = 'inline-flex';
        container.style.alignItems = 'center';
        container.style.justifyContent = 'center';
        container.appendChild(canvas);

        // Star animation state
        let time = 0;
        let animationId = null;
        const cx = config.size / 2;
        const cy = config.size / 2;

        function draw() {
            const ctx = canvas.getContext('2d');
            if (!ctx) return;

            // Clear canvas
            ctx.clearRect(0, 0, config.size, config.size);

            // Slow pulse (≈17-second cycle at speed=1)
            const intensity = 0.88 + 0.55 * (Math.sin(time * 0.0115 * config.speed) * 0.5 + 0.5);

            // Scale factor for size
            const scale = config.size / 100;

            // Pure white → clean blue
            const blueAmount = (intensity - 0.88) / 0.55;
            const r = Math.floor(255 * (1 - blueAmount * 0.48));
            const g = Math.floor(255 * (1 - blueAmount * 0.28));
            const b = 255;
            const coreColor = `rgb(${r},${g},${b})`;
            const glowColor = `rgba(${r},${g},${b},0.9)`;

            // Nebula glow
            const halo = ctx.createRadialGradient(cx, cy, 4 * scale, cx, cy, 56 * scale);
            halo.addColorStop(0, 'rgba(255,255,255,0.98)');
            halo.addColorStop(0.035, 'rgba(255,255,255,0.48)');
            halo.addColorStop(0.11, glowColor);
            halo.addColorStop(0.42, `rgba(${r},${g},${b},0.14)`);
            halo.addColorStop(0.75, 'transparent');
            ctx.fillStyle = halo;
            ctx.fillRect(0, 0, config.size, config.size);

            // Tiny bright white core
            ctx.shadowBlur = 14 * scale;
            ctx.shadowColor = '#ffffff';
            ctx.fillStyle = '#ffffff';
            ctx.beginPath();
            ctx.arc(cx, cy, 0.68 * scale * intensity, 0, Math.PI * 2);
            ctx.fill();

            // Soft white middle layer
            ctx.shadowBlur = 10 * scale;
            ctx.fillStyle = 'rgba(255,255,255,0.75)';
            ctx.beginPath();
            ctx.arc(cx, cy, 1.02 * scale * intensity, 0, Math.PI * 2);
            ctx.fill();

            // Blue core layer
            ctx.shadowBlur = 6 * scale;
            ctx.shadowColor = glowColor;
            ctx.fillStyle = coreColor;
            ctx.beginPath();
            ctx.arc(cx, cy, 1.62 * scale * intensity, 0, Math.PI * 2);
            ctx.fill();

            ctx.shadowBlur = 0;

            // Rays setup
            const maxLength = 96 * scale * intensity * 0.7;
            const rotation = Math.PI * 0.085;

            function drawFadingRay(angle, length, width, baseOpacity) {
                const x2 = cx + Math.cos(angle) * length;
                const y2 = cy + Math.sin(angle) * length;

                const gradient = ctx.createLinearGradient(cx, cy, x2, y2);
                gradient.addColorStop(0, `rgba(245, 250, 255, ${baseOpacity})`);
                gradient.addColorStop(1, 'transparent');

                ctx.strokeStyle = gradient;
                ctx.lineWidth = width * scale;
                ctx.lineCap = 'round';
                ctx.shadowBlur = 6 * scale;
                ctx.shadowColor = glowColor;

                ctx.beginPath();
                ctx.moveTo(cx, cy);
                ctx.lineTo(x2, y2);
                ctx.stroke();
            }

            // 4 main long rays
            const mainAngles = [0, Math.PI/2, Math.PI, 3*Math.PI/2].map(a => a + rotation);
            mainAngles.forEach(angle => {
                drawFadingRay(angle, maxLength, 2.1, 0.68);
                drawFadingRay(angle, maxLength * 0.94, 1.0, 0.9);
                drawFadingRay(angle, maxLength * 0.62, 0.4, 1);
            });

            // Inner rays (12)
            const innerRayCount = 12;
            for (let i = 0; i < innerRayCount; i++) {
                const baseAngle = (i * Math.PI * 2) / innerRayCount + rotation;
                const unevenAngle = Math.sin(i * 3.7) * 0.18;
                const unevenLength = 0.78 + Math.cos(i * 2.1) * 0.22;
                const angle = baseAngle + unevenAngle;

                const smallLength = maxLength * 0.46 * unevenLength;

                drawFadingRay(angle, smallLength, 1.0, 0.26);
                drawFadingRay(angle, smallLength * 0.86, 0.4, 0.44);
                drawFadingRay(angle, smallLength * 0.51, 0.2, 0.58);
            }

            // Extra tiniest inner rays (9)
            for (let i = 0; i < 9; i++) {
                const baseAngle = (i * Math.PI * 2) / 9 + rotation + 0.35;
                const unevenAngle = Math.sin(i * 4.2) * 0.25;
                const unevenLength = 0.62 + Math.sin(i * 1.8) * 0.31;
                const angle = baseAngle + unevenAngle;

                const tinyLength = maxLength * 0.26 * unevenLength;
                drawFadingRay(angle, tinyLength, 0.27, 0.32);
            }

            time += 1;
            animationId = requestAnimationFrame(draw);
        }

        // Start animation
        draw();

        // Return control object
        const starInstance = {
            canvas: canvas,
            container: container,
            config: config,
            stop: function() {
                if (animationId) {
                    cancelAnimationFrame(animationId);
                    animationId = null;
                }
            },
            start: function() {
                if (!animationId) draw();
            },
            destroy: function() {
                this.stop();
                container.innerHTML = originalContent;
                container.style.display = '';
            }
        };

        window.TwinklingStars.push(starInstance);
        return starInstance;
    }

    /**
     * Auto-initialize stars when DOM is ready
     * Looks for elements with class 'header-star' and replaces them
     */
    function autoInit() {
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', autoInit);
            return;
        }

        // Small delay to ensure DOM is fully ready
        setTimeout(function() {
            const starElements = document.querySelectorAll('.header-star');
            starElements.forEach(function(el) {
                // Check if not already initialized
                if (!el.querySelector('.twinkling-star-canvas')) {
                    createTwinklingStar(el, { size: 48, speed: 1 });
                }
            });
        }, 100);
    }

    // Expose global function
    window.createTwinklingStar = createTwinklingStar;

    // Auto-init
    autoInit();
})();
