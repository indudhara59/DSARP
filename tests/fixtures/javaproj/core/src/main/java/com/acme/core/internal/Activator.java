package com.acme.core.internal;

public class Activator {
    static {
        System.out.println("start");
    }

    public void start() {
        new com.acme.core.Engine().run(java.util.List.of("x"));
    }
}
