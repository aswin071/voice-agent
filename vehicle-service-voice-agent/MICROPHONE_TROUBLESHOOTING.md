# Microphone Issue Troubleshooting

## The Error
`Cannot read properties of undefined (reading 'getUserMedia')`

This means `navigator.mediaDevices` is **undefined**.

## Common Causes & Fixes

### 1. **Not Using HTTP Server** (Most Common)
**Problem:** Opening file directly (`file:///path/to/test.html`)

**Fix:**
```bash
python3 -m http.server 3000
# Then open: http://localhost:3000/test_simple.html
```

### 2. **Not localhost or HTTPS**
**Problem:** Using IP address like `http://192.168.1.5:3000`

**Fix:** Use `http://localhost:3000` NOT the IP address

### 3. **Browser Blocking**
**Problem:** Browser security settings

**Fix for Chrome:**
1. Go to: `chrome://flags/#unsafely-treat-insecure-origin-as-secure`
2. Enable it
3. Add: `http://localhost:3000`
4. Restart Chrome

### 4. **Incognito/Private Mode**
**Problem:** Some browsers block media in private mode

**Fix:** Use normal browser window

### 5. **HTTP vs HTTPS**
**Problem:** Modern browsers require HTTPS for getUserMedia (except localhost)

**Fix:** Use localhost or set up HTTPS with ngrok:
```bash
ngrok http 3000
# Use the https:// URL provided
```

## Quick Test Steps

1. **Start server:**
   ```bash
   cd /home/wac/vehicle-service-voice-agent/vehicle-service-voice-agent
   python3 -m http.server 3000
   ```

2. **Open in Chrome:**
   ```
   http://localhost:3000/test_simple.html
   ```

3. **Click "Check Support"**
   - Should show: `navigator.mediaDevices: YES`
   - If `NO`, check protocol is `http:` and host is `localhost:3000`

4. **Click "Test Microphone"**
   - Allow when browser asks
   - Speak and see audio levels

5. **If all above works, then try "Connect to LiveKit"**

## Diagnostic Commands

```bash
# Check if running on correct URL
curl -s http://localhost:3000/test_simple.html | head -5

# Should return HTML, not "Failed to connect"
```

## Still Not Working?

Try this minimal test directly in browser console (F12):

```javascript
// Open http://localhost:3000 in browser
// Press F12, go to Console, paste:

console.log('mediaDevices:', typeof navigator.mediaDevices);
console.log('secure context:', window.isSecureContext);
console.log('protocol:', window.location.protocol);

// Try getUserMedia
navigator.mediaDevices.getUserMedia({ audio: true })
  .then(s => console.log('SUCCESS!'))
  .catch(e => console.log('ERROR:', e.name));
```

**Expected output:**
```
mediaDevices: object
secure context: true
protocol: http:
SUCCESS!
```

If you see `mediaDevices: undefined`, the page isn't being served correctly.
