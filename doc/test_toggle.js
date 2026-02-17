// Simple test to verify category toggle works
document.addEventListener('DOMContentLoaded', function() {
    const categories = document.querySelectorAll('.category');
    console.log(`Found ${categories.length} categories`);
    
    categories.forEach((category, idx) => {
        const header = category.querySelector('.category-header');
        const content = category.querySelector('.category-content');
        console.log(`Category ${idx}: header=${!!header}, content=${!!content}`);
        
        if (header) {
            header.addEventListener('click', function(e) {
                e.stopPropagation();
                console.log('Header clicked for category', idx);
                
                if (content) {
                    const isExpanded = category.classList.contains('expanded');
                    console.log('Currently expanded:', isExpanded);
                    
                    // Close all others
                    categories.forEach((c, cidx) => {
                        if (cidx !== idx) {
                            c.classList.remove('expanded');
                            const ccontent = c.querySelector('.category-content');
                            if (ccontent) ccontent.style.maxHeight = null;
                        }
                    });
                    
                    // Toggle
                    if (!isExpanded) {
                        category.classList.add('expanded');
                        content.style.maxHeight = content.scrollHeight + 'px';
                        console.log('Expanded category', idx);
                    } else {
                        category.classList.remove('expanded');
                        content.style.maxHeight = null;
                        console.log('Collapsed category', idx);
                    }
                }
            });
        }
    });
});
