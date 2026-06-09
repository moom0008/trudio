(function(){
  function sanitizeFilename(name){
    if (!name) return 'output';
    return name.replace(/[<>:"\/\\|?*\x00-\x1F]+/g,'').trim() || 'output';
  }

  function normalizeHTML(html){
    // 2+ <br> => newline; single <br> => space
    html = html.replace(/(\s*<br\s*\/?>\s*){2,}/gi, '\n');
    html = html.replace(/<br\s*\/?>/gi, ' ');
    return html;
  }

  function downloadOrFallback(filename, text){
    try{
      var blob = new Blob([text], {type: 'text/plain;charset=utf-8'});
      var url = URL.createObjectURL(blob);
      var a = document.createElement('a');
      a.href = url;
      a.download = filename;
      document.body.appendChild(a);
      a.click();
      setTimeout(function(){ URL.revokeObjectURL(url); a.remove(); }, 500);
    }catch(e){
      console.warn('Download blocked, copying content to clipboard instead.', e);
      if (navigator.clipboard && navigator.clipboard.writeText){
        navigator.clipboard.writeText(text).then(function(){
          console.log('Content copied to clipboard.');
        }, function(err){
          console.error('Failed to copy to clipboard:', err);
          console.log('---- FILE:', filename, '----\n' + text);
        });
      }else{
        console.log('---- FILE:', filename, '----\n' + text);
      }
    }
  }

  var nameNode = document.querySelector('div.name[calculated="true"]'); // tiêu đề chương
  var baseName = sanitizeFilename(nameNode ? nameNode.textContent : 'output');
  var contentNodes = Array.from(document.querySelectorAll('div.content[calculated="true"]')); // nội dung chương
  if (!contentNodes.length){ console.warn('Extractor: Không tìm thấy thẻ div.content[calculated=\"true\"].'); return; }

  contentNodes.forEach(function(node, idx){
    var style = node.getAttribute('style') || '';
    if (!/font-family\s*:\s*arial/i.test(style)) return; // chỉ xử lý font-family: arial
    var h = node.innerHTML;
    h = normalizeHTML(h);
    var tmp = document.createElement('div'); tmp.innerHTML = h;
    var text = tmp.textContent.replace(/\u00A0/g,' ').split('\n').map(function(s){ return s.replace(/\s+/g,' ').trim(); }).join('\n').trim();
    var filename = baseName + (contentNodes.length>1 ? ('-' + (idx+1)) : '') + '.txt';
    downloadOrFallback(filename, text);
  });

  // After downloads, wait 2 seconds then try to click the next-chapter link
  setTimeout(function(){
    try{
      var next = document.querySelector('a.next-chapter[calculated="true"]'); // link chương tiếp theo
      if (!next){ console.warn('Extractor: no next-chapter link found to click.'); return; }
      try{ next.click(); }
      catch(e){
        // fallback: navigate to href
        var href = next.getAttribute('href');
        if (href) location.href = new URL(href, location.href).href;
      }
    }catch(err){ console.error('Extractor: error while trying to click next link', err); }
  }, 2000);
})();