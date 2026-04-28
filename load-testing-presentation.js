class SlidePresentation {
    constructor() {
        this.slides = Array.from(document.querySelectorAll('.slide'));
        this.currentSlide = 0;
        this.isAnimating = false;

        this.progressBar = document.getElementById('progressBar');
        this.navDotsContainer = document.getElementById('navDots');

        this.setupNavDots();
        this.setupIntersectionObserver();
        this.setupKeyboardNav();
        this.setupTouchNav();
        this.setupWheelNav();
        this.setupTermTips();
        this.setupProgressBar();
    }

    /* ---------------------------
       Generate nav dots — clear first to avoid duplicates
       on file re-open (outerHTML capture would otherwise
       persist the rendered dots on disk)
       --------------------------- */
    setupNavDots() {
        this.navDotsContainer.innerHTML = '';
        this.slides.forEach((_, idx) => {
            const dot = document.createElement('button');
            dot.setAttribute('aria-label', `Go to slide ${idx + 1}`);
            if (idx === 0) dot.classList.add('active');
            dot.addEventListener('click', () => this.goToSlide(idx));
            this.navDotsContainer.appendChild(dot);
        });
    }

    /* ---------------------------
       IntersectionObserver — adds .visible class so
       CSS reveal transitions fire as slide enters view
       --------------------------- */
    setupIntersectionObserver() {
        const observer = new IntersectionObserver((entries) => {
            entries.forEach(entry => {
                if (entry.isIntersecting && entry.intersectionRatio > 0.5) {
                    entry.target.classList.add('visible');
                    const idx = this.slides.indexOf(entry.target);
                    if (idx !== -1) {
                        this.currentSlide = idx;
                        this.updateNavDots();
                        this.updateProgress();
                    }
                }
            });
        }, { threshold: [0, 0.5, 1] });

        this.slides.forEach(slide => observer.observe(slide));
    }

    setupKeyboardNav() {
        document.addEventListener('keydown', (e) => {
            if (e.target.closest('button, a, input, textarea, select')) return;

            switch (e.key) {
                case 'ArrowDown': case 'ArrowRight':
                case 'PageDown': case ' ':
                    e.preventDefault(); this.next(); break;
                case 'ArrowUp': case 'ArrowLeft': case 'PageUp':
                    e.preventDefault(); this.prev(); break;
                case 'Home':
                    e.preventDefault(); this.goToSlide(0); break;
                case 'End':
                    e.preventDefault(); this.goToSlide(this.slides.length - 1); break;
            }
        });
    }

    setupTouchNav() {
        let touchStartY = 0;
        let touchStartX = 0;
        document.addEventListener('touchstart', (e) => {
            touchStartY = e.touches[0].clientY;
            touchStartX = e.touches[0].clientX;
        }, { passive: true });
        document.addEventListener('touchend', (e) => {
            const dy = e.changedTouches[0].clientY - touchStartY;
            const dx = e.changedTouches[0].clientX - touchStartX;
            if (Math.abs(dy) < 50 && Math.abs(dx) < 50) return;
            if (Math.abs(dy) > Math.abs(dx)) {
                if (dy < 0) this.next(); else this.prev();
            }
        }, { passive: true });
    }

    /* ---------------------------
       Wheel nav with debounce — scroll snap handles
       most cases, this prevents over-scrolling past slides
       --------------------------- */
    setupWheelNav() {
        let wheelTimeout = null;
        document.addEventListener('wheel', (e) => {
            if (this.isAnimating) return;
            clearTimeout(wheelTimeout);
            wheelTimeout = setTimeout(() => {
                this.isAnimating = false;
            }, 250);
        }, { passive: true });
    }

    setupTermTips() {
        const tips = Array.from(document.querySelectorAll('.term-tip'));
        if (tips.length === 0) return;

        const closeAll = () => tips.forEach(tip => tip.classList.remove('is-open'));

        tips.forEach(tip => {
            tip.addEventListener('click', (e) => {
                e.stopPropagation();
                const wasOpen = tip.classList.contains('is-open');
                closeAll();
                if (!wasOpen) tip.classList.add('is-open');
            });
        });

        document.addEventListener('click', closeAll);
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') closeAll();
        });
    }

    setupProgressBar() {
        this.updateProgress();
    }

    updateProgress() {
        const pct = (this.currentSlide / Math.max(1, this.slides.length - 1)) * 100;
        this.progressBar.style.width = pct + '%';
    }

    updateNavDots() {
        const dots = this.navDotsContainer.querySelectorAll('button');
        dots.forEach((d, i) => d.classList.toggle('active', i === this.currentSlide));
    }

    next() {
        if (this.currentSlide < this.slides.length - 1) {
            this.goToSlide(this.currentSlide + 1);
        }
    }
    prev() {
        if (this.currentSlide > 0) {
            this.goToSlide(this.currentSlide - 1);
        }
    }
    goToSlide(idx) {
        idx = Math.max(0, Math.min(idx, this.slides.length - 1));
        this.slides[idx].scrollIntoView({ behavior: 'smooth', block: 'start' });
        this.currentSlide = idx;
        this.updateNavDots();
        this.updateProgress();
    }
}

/* Initialize after DOM ready */
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => new SlidePresentation());
} else {
    new SlidePresentation();
}

/* First slide should be visible immediately on load */
document.querySelector('.slide').classList.add('visible');
