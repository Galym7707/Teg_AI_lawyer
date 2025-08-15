const BACKEND_URL = process.env.BACKEND_URL || 'http://localhost:5000';

export default async function handler(req, res) {
  const { query } = req;
  const path = Array.isArray(query.path) ? query.path.join('/') : query.path || '';

  const url = `${BACKEND_URL}/${path}`;

  try {
    // Handle CORS preflight
    if (req.method === 'OPTIONS') {
      res.setHeader('Access-Control-Allow-Origin', '*');
      res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, DELETE');
      res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
      res.setHeader('Access-Control-Allow-Credentials', 'true');
      res.setHeader('Access-Control-Max-Age', '86400');
      return res.status(200).end();
    }

    const headers = {
      'Content-Type': req.headers['content-type'] || 'application/json',
    };

    // Forward other headers if needed
    if (req.headers.authorization) {
      headers.Authorization = req.headers.authorization;
    }

    const fetchOptions = {
      method: req.method,
      headers,
    };

    // Add body for non-GET requests
    if (req.method !== 'GET' && req.method !== 'HEAD' && req.body) {
      if (typeof req.body === 'string') {
        fetchOptions.body = req.body;
      } else {
        fetchOptions.body = JSON.stringify(req.body);
      }
    }

    const backendResponse = await fetch(url, fetchOptions);

    // Set CORS headers
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.setHeader('Access-Control-Allow-Methods', 'GET, POST, OPTIONS, DELETE');
    res.setHeader('Access-Control-Allow-Headers', 'Content-Type, Authorization');
    res.setHeader('Access-Control-Allow-Credentials', 'true');

    // Forward response headers
    const contentType = backendResponse.headers.get('Content-Type');
    if (contentType) {
      res.setHeader('Content-Type', contentType);
    }

    // Set status
    res.status(backendResponse.status);

    // Handle streaming responses
    if (backendResponse.body) {
      const reader = backendResponse.body.getReader();
      
      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        res.write(value);
      }
      res.end();
    } else {
      res.end();
    }

  } catch (error) {
    console.error('Proxy error:', error);
    res.setHeader('Access-Control-Allow-Origin', '*');
    res.status(500).json({ error: 'Proxy error', details: error.message });
  }
}


