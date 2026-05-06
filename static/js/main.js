// Main JavaScript

document.addEventListener('DOMContentLoaded', function() {
    // Initialize components
    initNavigation();
    initFlashMessages();
});

function initNavigation() {
    // Mobile menu toggle
    const menuToggle = document.querySelector('.menu-toggle');
    if (menuToggle) {
        menuToggle.addEventListener('click', toggleMobileMenu);
    }
}

function initFlashMessages() {
    // Auto-dismiss flash messages
    const alerts = document.querySelectorAll('.alert');
    alerts.forEach(alert => {
        setTimeout(() => {
            alert.style.opacity = '0';
            setTimeout(() => alert.remove(), 300);
        }, 5000);
    });
}

function toggleMobileMenu() {
    const nav = document.querySelector('.nav-links');
    nav.classList.toggle('show');
}

async function apiRequest(url, options = {}) {
    const defaultOptions = {
        credentials: 'include',
        headers: {
            'Content-Type': 'application/json',
        }
    };
    
    const response = await fetch(url, { ...defaultOptions, ...options });
    const data = await response.json();
    
    if (!response.ok) {
        throw new Error(data.message || 'Request failed');
    }
    
    return data;
}