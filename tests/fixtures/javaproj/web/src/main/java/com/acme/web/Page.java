package com.acme.web;

import com.acme.core.Engine;
import com.acme.core.Service;
import org.external.Missing;

public class Page {
    private Service service = new Engine();
    private Missing missing;

    public String render() {
        return service.run(java.util.List.of("a")) + com.acme.util.Dates.PREFIX;
    }
}
